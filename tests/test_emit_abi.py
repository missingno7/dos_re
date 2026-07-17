"""ABI-recovered emitter (dos_re.lift.emit_abi) -- M3b slice 1.

Generates the dual-entrypoint module for a census proposal and executes it
against a stub mechanical core: the public entry must project the semantic
contract; the shadow must preserve the mechanical ABI and XOR-perturb
exactly the dropped outputs.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.lift import emit_abi
from dos_re.lift.contracts import infer_contracts
from dos_re.lift.emit_cpuless import Refusal
from tests.test_contracts import _ir, CALLER, CALLEE


def _census():
    return infer_contracts(_ir({"1010:0000": CALLER, "1010:0100": CALLEE}))


def _load(key, src, core):
    """Exec a generated module with a stub package supplying the core."""
    stem = emit_abi._stem(key)
    pkg = types.ModuleType("stub_recovered")
    coremod = types.ModuleType(f"stub_recovered.func_{stem}")
    setattr(coremod, f"func_{stem}", core)
    sys.modules["stub_recovered"] = pkg
    sys.modules[f"stub_recovered.func_{stem}"] = coremod
    mod = types.ModuleType(f"abi_{stem}")
    exec(compile(src, f"abi_{stem}.py", "exec"), mod.__dict__)
    return mod


def test_public_entry_projects_semantic_contract():
    prop = _census()["functions"]["1010:0100"]
    src = emit_abi.emit_abi_module("1010:0100", prop,
                                   import_base="stub_recovered")
    calls = {}

    def core(mem, **kw):
        calls.update(kw)
        return ({"ax": 5, "bx": 7}, {"flags": 0, "fmask": 0, "cost": 3})

    mod = _load("1010:0100", src, core)
    # the callee's contract: no semantic params, machine stack only,
    # observed return = ax alone
    out = mod.abi_1010_0100("MEM", stack=(0x2000, 0x100))
    assert out == 5
    assert calls.get("ss") == 0x2000 and calls.get("sp") == 0x100


def test_shadow_poisons_exactly_the_dropped_outputs():
    prop = _census()["functions"]["1010:0100"]
    assert prop["dropped_outputs"] == ["bx"]
    src = emit_abi.emit_abi_module("1010:0100", prop,
                                   import_base="stub_recovered")

    def core(mem, **kw):
        return ({"ax": 5, "bx": 7}, {"flags": 0, "fmask": 0, "cost": 3})

    mod = _load("1010:0100", src, core)
    out, compat = mod.func_1010_0100("MEM", ss=1, sp=2)
    assert out["ax"] == 5                      # observed: untouched
    assert out["bx"] == 7 ^ emit_abi.POISON_XOR    # dropped: perturbed
    assert compat["cost"] == 3                 # compat channel intact


def test_refused_proposal_does_not_emit():
    census = infer_contracts(_ir({"1010:0000": "74 01 C3 CB"}))
    prop = census["functions"]["1010:0000"]
    assert prop["refusals"]
    with pytest.raises(Refusal):
        emit_abi.emit_abi_module("1010:0000", prop,
                                 import_base="stub_recovered")


def test_shadow_loader_aliases_and_retro_patches():
    src = emit_abi.emit_shadow_loader(["1010:0100"],
                                      abi_base="stub_abi",
                                      import_base="stub_recovered")

    # the ORIGINAL mechanical callee + a caller that already bound it at
    # module level (the stale-binding hazard the loader must repair)
    def original(mem, **kw):
        return {"ax": 1}, {}

    mech = types.ModuleType("stub_recovered.func_1010_0100")
    mech.func_1010_0100 = original
    caller = types.ModuleType("stub_recovered.func_1010_0200")
    caller.func_1010_0100 = original          # from ... import func_1010_0100

    def shadow_fn(mem, *args, **kw):
        return {"ax": 0xBAD}, {}

    shadow = types.ModuleType("stub_abi.abi_1010_0100")
    shadow.func_1010_0100 = shadow_fn
    shadow._core = original                    # __globals__ -> mech namespace
    original.__globals__["func_1010_0100"] = original

    sys.modules["stub_abi"] = types.ModuleType("stub_abi")
    sys.modules["stub_abi.abi_1010_0100"] = shadow
    sys.modules["stub_recovered.func_1010_0100"] = mech
    sys.modules["stub_recovered.func_1010_0200"] = caller

    mod = types.ModuleType("shadow_loader")
    exec(compile(src, "shadow_loader.py", "exec"), mod.__dict__)
    n, patched = mod.install_shadows()
    assert n == 1
    # future imports resolve to the shadow
    assert sys.modules["stub_recovered.func_1010_0100"] is shadow
    # the caller's stale module-level binding was rebound
    assert caller.func_1010_0100 is shadow_fn
    assert patched >= 1
