"""dos_re.lift.standalone -- the shared standalone CPUless host (the wall, the loader, the platform).

Every port needs the same four things to run a promoted corpus as a program, and each had grown its own
copy. These pin the shared contract: the import wall refuses the carrier (including through RELATIVE
imports, the blind spot that once let dos_re.cpu in silently), the loader fails LOUD on a frontier
function instead of falling back, the default platform names the missing device, and run_deep gives a
bounded tail-dispatch loop the headroom to finish.
"""
from __future__ import annotations

import builtins
import sys
import types

import pytest

from dos_re.lift.standalone import (BASE_FORBIDDEN, CpuStandaloneWitness, FailLoudPlatform,
                                    forbidden_hit, install_import_guard, load_recovered,
                                    module_name, resolve_import, run_deep, run_recovered)


def test_module_name_mapping():
    assert module_name("1010:5F61") == "func_1010_5f61"
    assert module_name("254A:04D7") == "func_254a_04d7"


def test_forbidden_hit_is_package_prefix_matched():
    assert forbidden_hit("dos_re.cpu", BASE_FORBIDDEN) == "dos_re.cpu"
    assert forbidden_hit("dos_re.cpu.sub", BASE_FORBIDDEN) == "dos_re.cpu"   # submodule
    assert forbidden_hit("dos_re.cpuxyz", BASE_FORBIDDEN) is None            # not a prefix match
    assert forbidden_hit("dos_re.memory", BASE_FORBIDDEN) is None            # permitted
    assert forbidden_hit("port.adapters.x", ("port.adapters",)) == "port.adapters"   # extra_forbidden


def test_resolve_relative_import():
    g = {"__package__": "dos_re.lift"}
    assert resolve_import("cpu", g, 1) == "dos_re.lift.cpu"   # from .cpu
    assert resolve_import("cpu", g, 2) == "dos_re.cpu"        # from ..cpu -- the real blind spot
    assert resolve_import("dos_re.cpu", g, 0) == "dos_re.cpu"


def test_guard_refuses_carrier_and_extra_forbidden():
    saved = builtins.__import__
    try:
        install_import_guard(extra_forbidden=("myport.cpuless_adapters",))
        with pytest.raises(CpuStandaloneWitness):
            __import__("dos_re.cpu")
        with pytest.raises(CpuStandaloneWitness):
            __import__("myport.cpuless_adapters")
        __import__("json")                     # a permitted import still works
    finally:
        builtins.__import__ = saved


def test_fail_loud_platform_names_the_missing_service():
    plat = FailLoudPlatform()
    for call in (lambda: plat.intr(0x10, {}, 0),
                 lambda: plat.inp(0x3DA, 1, 0),
                 lambda: plat.outp(0x3D8, 0, 1, 0)):
        with pytest.raises(CpuStandaloneWitness):
            call()


def test_loader_fails_loud_on_a_frontier_function():
    with pytest.raises(CpuStandaloneWitness):
        load_recovered("dos_re.lift", "1010:DEAD")     # no such recovered module


def test_run_recovered_calls_the_corpus_module(monkeypatch):
    # a synthetic one-module corpus: run_recovered must import it by CS:IP and return outputs only.
    pkg = types.ModuleType("synthcorpus")
    pkg.__path__ = []
    mod = types.ModuleType("synthcorpus.func_1010_1234")

    def func_1010_1234(mem, plat, *, ax=0):
        mem.append(("ran", ax))
        return {"ax": (ax + 1) & 0xFFFF}, {"cost": 1}

    mod.func_1010_1234 = func_1010_1234
    monkeypatch.setitem(sys.modules, "synthcorpus", pkg)
    monkeypatch.setitem(sys.modules, "synthcorpus.func_1010_1234", mod)

    log = []
    out = run_recovered("synthcorpus", "1010:1234", log, ax=7)
    assert out == {"ax": 8} and log == [("ran", 7)]


def test_run_deep_completes_a_deep_recursion_and_propagates():
    def deep(n):
        return 0 if n == 0 else 1 + deep(n - 1)

    # far beyond the default limit (1000): a bounded tail-dispatch loop looks exactly like this.
    assert run_deep(deep, 20000) == 20000

    def boom():
        raise ValueError("propagated verbatim")

    with pytest.raises(ValueError, match="propagated verbatim"):
        run_deep(boom)
