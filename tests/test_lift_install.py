"""Focused tests for dos_re.lift.install — the hybrid auto-install rung.

Only proven routines install; the fingerprint is deterministic and covers
both which addresses are hooked and which module answers each (so a demo can
detect a changed lift set and refuse to desync).  Game-free (synthetic)."""
from __future__ import annotations

import json
from pathlib import Path

from dos_re.lift.install import (planned_lifts, install_passing_lifts,
                                 lift_fingerprint, passing_entries)


def _manifest(tmp_path, name, recs):
    p = tmp_path / name
    p.write_text(json.dumps({r["entry"]: r for r in recs}))
    return p


def test_only_passing_and_right_segment_are_planned(tmp_path):
    m = _manifest(tmp_path, "m.json", [
        {"entry": "1010:0100", "module": "lifted_1010_0100.py", "status": "ORACLE_PASSING"},
        {"entry": "1010:0200", "module": "lifted_1010_0200.py", "status": "NOT_REACHED"},
        {"entry": "1010:0300", "module": "lifted_1010_0300.py", "status": "DIVERGED"},
        {"entry": "2911:0000", "module": "lifted_2911_0000.py", "status": "ORACLE_PASSING"},
    ])
    plan = planned_lifts(0x1010, [m])
    assert plan == {(0x1010, 0x0100): "lifted_1010_0100.py"}   # passing + seg only


def test_skip_excludes_and_merge_prefers_passing(tmp_path):
    m1 = _manifest(tmp_path, "m1.json", [
        {"entry": "1010:0100", "module": "lifted_1010_0100.py", "status": "NOT_REACHED"}])
    m2 = _manifest(tmp_path, "m2.json", [
        {"entry": "1010:0100", "module": "lifted_1010_0100.py", "status": "ORACLE_PASSING"},
        {"entry": "1010:0400", "module": "lifted_1010_0400.py", "status": "ORACLE_PASSING"}])
    assert set(passing_entries([m1, m2])) == {"1010:0100", "1010:0400"}
    plan = planned_lifts(0x1010, [m1, m2], skip=("1010:0400",))
    assert plan == {(0x1010, 0x0100): "lifted_1010_0100.py"}


def test_fingerprint_is_deterministic_and_module_sensitive(tmp_path):
    a = {(0x1010, 0x0100): "lifted_1010_0100.py", (0x1010, 0x0200): "lifted_1010_0200.py"}
    b = {(0x1010, 0x0200): "lifted_1010_0200.py", (0x1010, 0x0100): "lifted_1010_0100.py"}
    assert lift_fingerprint(a) == lift_fingerprint(b)         # order-independent
    assert lift_fingerprint({}) == ""                        # hook-free demo
    c = dict(a); c[(0x1010, 0x0100)] = "lifted_1010_0100_v2.py"
    assert lift_fingerprint(c) != lift_fingerprint(a)        # module change → new fp


def test_install_registers_hooks_on_cpu(tmp_path):
    (tmp_path / "lifted_1010_0100.py").write_text(
        "def lifted_1010_0100(cpu):\n    cpu.s.ax = 0x1234\n")
    m = _manifest(tmp_path, "m.json", [
        {"entry": "1010:0100", "module": "lifted_1010_0100.py", "status": "ORACLE_PASSING"}])

    class FakeCPU:
        def __init__(self):
            self.replacement_hooks = {}
            self.hook_names = {}
    cpu = FakeCPU()
    installed = install_passing_lifts(cpu, 0x1010, tmp_path, [m])
    assert installed == {(0x1010, 0x0100): "lifted_1010_0100.py"}
    assert (0x1010, 0x0100) in cpu.replacement_hooks
    assert cpu.hook_names[(0x1010, 0x0100)] == "lifted_1010_0100"
