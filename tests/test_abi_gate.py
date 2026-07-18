"""The M3b static wall (tools/abi_gate.py): every counter must be zero, and
each violation must NAME its file.  These lock the shapes the milestone
forbids -- including the two defects that actually occurred during the
slice work (an unbound composed-call argument, and a stale core module left
on disk after its function was refused).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_GATE = Path(__file__).resolve().parents[1] / "tools" / "abi_gate.py"
_spec = importlib.util.spec_from_file_location("abi_gate", _GATE)
abi_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(abi_gate)

_CLEAN_CORE = '''\
"""generated"""
_CONTRACT = {
    'key': '1010:0100',
    'params': ({'name': 'arg_0', 'role': 'segment', 'historical': 'ds'},),
    'returns': ({'name': 'ret_0', 'historical': 'ax'},),
}


def _abi_core(mem, arg_0=0):
    ds = arg_0
    ax = mem.rw(ds, 4)
    return (ax & 0xFFFF,), {'flags': 0, 'fmask': 0, 'cost': 1}
'''


def _write(tmp_path, name, src, keys):
    d = tmp_path / "abi"
    d.mkdir(exist_ok=True)
    (d / name).write_text(src, encoding="utf-8")
    (d / "cores_manifest.json").write_text(
        json.dumps({"cores": keys}), encoding="utf-8")
    return d


_CENSUS = {"functions": {"1010:0100": {"refusals": []}}}


def test_clean_core_passes_every_counter(tmp_path):
    d = _write(tmp_path, "core_1010_0100.py", _CLEAN_CORE, ["1010:0100"])
    rep = abi_gate.gate(d, _CENSUS)
    assert rep["cores"] == 1
    assert all(not v for v in rep["counters"].values()), rep["counters"]


def test_stale_core_module_is_flagged(tmp_path):
    """A core file whose function is no longer in the manifest was REFUSED
    but left on disk -- it can still be imported by a sibling and silently
    shadow the refusal."""
    d = _write(tmp_path, "core_1010_0100.py", _CLEAN_CORE, [])
    rep = abi_gate.gate(d, _CENSUS)
    assert rep["counters"]["stale_core_modules"] == ["core_1010_0100.py"]


def test_register_named_public_param_is_flagged(tmp_path):
    src = _CLEAN_CORE.replace("def _abi_core(mem, arg_0=0):",
                              "def _abi_core(mem, ax=0):")
    d = _write(tmp_path, "core_1010_0100.py", src, ["1010:0100"])
    rep = abi_gate.gate(d, _CENSUS)
    assert rep["counters"]["register_named_public_params"]


def test_unbound_composed_call_argument_is_flagged(tmp_path):
    """The real slice-8 defect: a callee taking ss as a semantic segment,
    composed by a caller that never binds ss."""
    src = _CLEAN_CORE.replace(
        "    ax = mem.rw(ds, 4)",
        "    _o, _c = _core_1010_0200(mem, ss)\n    ax = _o[0]")
    d = _write(tmp_path, "core_1010_0100.py", src, ["1010:0100"])
    rep = abi_gate.gate(d, _CENSUS)
    assert any("ss" in h
               for h in rep["counters"]["unbound_composed_call_args"])


def test_machine_stack_memory_access_is_flagged_but_ss_as_data_is_not(tmp_path):
    # ss NOT a contract parameter -> a machine-stack access (or unbound)
    src = _CLEAN_CORE.replace("    ax = mem.rw(ds, 4)",
                              "    ss = 0x30\n    ax = mem.rw(ss, 4)")
    d = _write(tmp_path, "core_1010_0100.py", src, ["1010:0100"])
    rep = abi_gate.gate(d, _CENSUS)
    assert rep["counters"]["historical_stack_memory_access"]
    # ss DECLARED as a semantic segment parameter -> the ss-as-data case,
    # no more "stack" than a ds: access
    src2 = _CLEAN_CORE.replace(
        "{'name': 'arg_0', 'role': 'segment', 'historical': 'ds'},",
        "{'name': 'arg_0', 'role': 'segment', 'historical': 'ss'},"
    ).replace("    ds = arg_0\n    ax = mem.rw(ds, 4)",
              "    ss = arg_0\n    ax = mem.rw(ss, 4)")
    d2 = _write(tmp_path, "core_1010_0100.py", src2, ["1010:0100"])
    rep2 = abi_gate.gate(d2, _CENSUS)
    assert not rep2["counters"]["historical_stack_memory_access"]
    assert not rep2["counters"]["unbound_composed_call_args"]


def test_virtual_stack_and_return_address_writes_are_flagged(tmp_path):
    src = _CLEAN_CORE.replace(
        "    ax = mem.rw(ds, 4)",
        "    _vs = []\n    mem.ww(ss, sp, 0x1234)\n    ax = 0")
    d = _write(tmp_path, "core_1010_0100.py", src, ["1010:0100"])
    rep = abi_gate.gate(d, _CENSUS)
    assert rep["counters"]["virtual_stack_objects"]
    assert rep["counters"]["return_address_writes"]
