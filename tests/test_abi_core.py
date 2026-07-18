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

    abi_src, _contract = emit_abi.emit_abi_core(scan, prop, key)
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
    # the ABI core wrote NOTHING to memory (its stack is gone)
    m = TraceMem(7)
    core(m, 1, 2)          # positional: (ax, bx) in contract order
    assert m.writes == []


def test_no_stack_object_survives_in_the_body():
    """The proven depth map resolves push/pop to numbered slot LOCALS -- no
    list, no runtime stack discipline, in the emitted core."""
    ir = _ir({"1010:0300": LEAF_PUSH})
    scans, _ = build_scans(ir)
    prop = infer_contracts(ir)["functions"]["1010:0300"]
    src, _ = emit_abi.emit_abi_core(scans["1010:0300"], prop, "1010:0300")
    assert "_vs" not in src and ".append(" not in src and ".pop()" not in src
    assert "_slot_0 = bx & 0xFFFF" in src and "bx = _slot_0" in src


def test_destacked_core_matches_mechanical_branchy_memwrite():
    mech, core, prop, mod = _emit_pair("1010:0400", LEAF_BR)
    rep = diff_one(mech, core, prop, states=48)
    assert rep["ok"], rep["mismatches"][:3]
    # semantic memory writes DO happen (and are compared by the diff)
    m = TraceMem(7)
    core(m, 1, 0x10, 0x1234)   # positional: (ax, di, ds)
    assert m.writes == [(0x1234, 0x10, 1, 2)]


def test_public_entry_over_the_one_core():
    _, core, prop, mod = _emit_pair("1010:0300", LEAF_PUSH)
    pub = getattr(mod, "abi_1010_0300")
    out = pub(TraceMem(3), 2, 9)
    o, _ = core(TraceMem(3), 2, 9)
    assert out == (o if len(prop["returns"]) != 1 else o[0])


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


def test_portio_core_threads_plat_and_matches_mechanical():
    # in al,0x60; out 0x61,al; ret -- reads the keyboard port, echoes it
    mech, core, prop, mod = _emit_pair("1010:0500", "E4 60 E6 61 C3")
    rep = diff_one(mech, core, prop, states=32)
    assert rep["ok"], rep["mismatches"][:3]
    # the core takes the platform interface (mem, plat) and routes the
    # port access through it (no direct hardware in the recovered body)
    import inspect
    assert "plat" in inspect.signature(core).parameters
    src = emit_abi.emit_abi_core(scans_for("1010:0500", "E4 60 E6 61 C3"),
                                 prop, "1010:0500")[0]
    assert "plat.inp(0x60" in src and "plat.outp(0x61" in src


def scans_for(key, hexbytes):
    ir = _ir({key: hexbytes})
    return build_scans(ir)[0][key]


def test_platform_int_core_matches_mechanical():
    # mov ah,0x2c; int 0x21; ret -- DOS get-time service
    mech, core, prop, mod = _emit_pair("1010:0600", "B4 2C CD 21 C3")
    rep = diff_one(mech, core, prop, states=32)
    assert rep["ok"], rep["mismatches"][:3]
    src = emit_abi.emit_abi_core(scans_for("1010:0600", "B4 2C CD 21 C3"),
                                 prop, "1010:0600")[0]
    assert "plat.intr(0x21" in src


def test_game_vectored_int_stays_mechanical():
    # int 0x61 (sound driver) -- not a platform service, needs _ivec dispatch
    ir = _ir({"1010:0000": "CD 61 C3"})
    scans, _ = build_scans(ir)
    with pytest.raises(Refusal, match="game-vectored-int"):
        emit_abi.check_composable(scans["1010:0000"])


#: callee 1010:0100 -- push bx; mov bx,3; add ax,bx; pop bx; ret (adds 3 to ax)
_C_CALLEE = "53 BB 03 00 01 D8 5B C3"
#: caller 1010:0000 -- mov ax,10; call 0100; add ax,1; ret (-> ax = 14)
_C_CALLER = "B8 0A 00 E8 FA 00 05 01 00 C3"


def test_near_call_composition_matches_mechanical():
    import sys as _sys
    import types as _types
    from dos_re.lift import emit_cpuless as _ec

    ir = _ir({"1010:0000": _C_CALLER, "1010:0100": _C_CALLEE})
    scans, _ = build_scans(ir)
    census = infer_contracts(ir)

    # --- ABI side: emit callee core, then caller core composing it ---
    ck = "1010:0100"
    csrc, ccontract = emit_abi.emit_abi_core(scans[ck], census["functions"][ck],
                                             ck, abi_base="abidiff")
    pkg = _types.ModuleType("abidiff")
    _sys.modules["abidiff"] = pkg
    cmod = _types.ModuleType("abidiff.core_1010_0100")
    exec(compile(csrc, "core_callee.py", "exec"), cmod.__dict__)
    _sys.modules["abidiff.core_1010_0100"] = cmod

    kk = "1010:0000"
    ksrc, _ = emit_abi.emit_abi_core(
        scans[kk], census["functions"][kk], kk,
        callees={0x0100: ccontract}, abi_base="abidiff")
    kmod = _types.ModuleType("abidiff.core_1010_0000")
    exec(compile(ksrc, "core_caller.py", "exec"), kmod.__dict__)

    # composed call, no ret-addr writes: 10 + 3 + 1 = 14, memory untouched
    m = TraceMem(1)
    o, c = kmod._abi_core(m)
    ax_at = list(census["functions"][kk]["returns"]).index("ax")
    assert o[ax_at] == 14
    assert m.writes == []

    # --- mechanical reference: compose the callee contract, same result ---
    cscan = scans[ck]
    cspec = _ec.check_promotable(cscan)
    cabi = cspec.abi
    cmech_src = _ec.emit_recovered(cscan, cabi, ck)
    cmech = _types.ModuleType("func_1010_0100")
    exec(compile(cmech_src, "m_callee.py", "exec"), cmech.__dict__)
    contract = _ec.CalleeContract(
        name="func_1010_0100",
        inputs=tuple(_ec._contract_inputs(cscan, cabi)),
        outputs=tuple(sorted((cabi.outputs & (frozenset(_ec.W16)
                     | frozenset({"ds", "es"}))) - frozenset({"sp"}))),
        exit_flags=cspec.exit_flags, needs_plat=cspec.needs_plat,
        ret_kind=cspec.ret_kind, df_livein=cspec.df_livein)
    kscan = scans[kk]
    kspec = _ec.check_promotable(kscan, callees={0x0100: contract})
    kmech_src = _ec.emit_recovered(kscan, kspec.abi, kk,
                                   callees={0x0100: contract},
                                   recovered_import_base="mech0")
    mpkg = _types.ModuleType("mech0")
    _sys.modules["mech0"] = mpkg
    _sys.modules["mech0.func_1010_0100"] = cmech
    kmech = _types.ModuleType("mech0.func_1010_0000")
    exec(compile(kmech_src, "m_caller.py", "exec"), kmech.__dict__)

    mo, mc = kmech.func_1010_0000(TraceMem(1), sp=0x1000, ss=0x7000)
    assert mo["ax"] == 14
    # observed returns + cost identical (the composed glue is exact)
    assert o[ax_at] == mo["ax"] and c["cost"] == mc["cost"]
