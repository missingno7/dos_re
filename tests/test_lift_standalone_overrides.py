"""The stitch seam: an override must reach callers that were ALREADY imported.

The generated corpus is the skeleton; an override is skin replacing what ONE
address means. The mechanism is module shadowing -- and shadowing alone has a
silent hole, which is why this test exists rather than a docstring.

A generated module binds its callees at import time
(``from pkg.func_1010_XXXX import func_1010_XXXX``), so replacing
``sys.modules`` only affects imports that have not happened yet. Any caller
imported earlier keeps a direct reference and the override does nothing for
every call through it -- silently. Resolving the original even causes this: it
IMPORTS the module, eagerly binding that module's own callees, so installing
several overrides along one call chain guarantees the later ones miss.

Found in skyroads, where a counter built on the un-patched seam reported ZERO
calls for functions a traceback proved were executing, and three successive
conclusions were drawn from that noise before the cause was found.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.lift import standalone as S

PKG = "fake_corpus"


@pytest.fixture
def corpus():
    """A two-module corpus where the caller binds the callee AT IMPORT TIME."""
    callee = types.ModuleType(f"{PKG}.func_1010_2000")
    def real_callee(mem, **kw):
        return ({"ax": 1}, {"cost": 1})
    callee.func_1010_2000 = real_callee

    caller = types.ModuleType(f"{PKG}.func_1010_1000")
    caller.func_1010_2000 = real_callee          # the import-time binding
    def real_caller(mem, **kw):
        return caller.func_1010_2000(mem, **kw)
    caller.func_1010_1000 = real_caller

    pkg = types.ModuleType(PKG)
    dyn = types.ModuleType(f"{PKG}._dyncall")
    dyn._cache = {("dyn", "1010:2000"): "stale"}
    for name, mod in ((PKG, pkg), (f"{PKG}.func_1010_1000", caller),
                      (f"{PKG}.func_1010_2000", callee), (f"{PKG}._dyncall", dyn)):
        sys.modules[name] = mod
    yield caller, callee, dyn, real_callee
    S.uninstall_overrides(PKG)
    for name in (PKG, f"{PKG}.func_1010_1000", f"{PKG}.func_1010_2000",
                 f"{PKG}._dyncall"):
        sys.modules.pop(name, None)


def test_override_reaches_a_caller_imported_before_installation(corpus):
    caller, _callee, _dyn, real_callee = corpus

    def override(mem, **kw):
        return ({"ax": 99}, {"cost": 1})

    assert caller.func_1010_2000 is real_callee
    S.install_overrides(PKG, {"1010:2000": override})
    assert caller.func_1010_2000 is override, (
        "the already-imported caller still holds the ORIGINAL callee -- the "
        "override would silently do nothing for every call through it")
    assert caller.func_1010_1000(None)[0]["ax"] == 99


def test_install_clears_the_dynamic_dispatch_memo(corpus):
    _caller, _callee, dyn, _real = corpus
    S.install_overrides(PKG, {"1010:2000": lambda mem, **kw: ({}, {})})
    assert dyn._cache == {}, (
        "_dyncall memoises the resolved closure on first call, so an override "
        "installed later would never be seen through dynamic transfers")


def test_generated_stays_reachable_for_delegation(corpus):
    _caller, _callee, _dyn, real_callee = corpus
    S.install_overrides(PKG, {"1010:2000": lambda mem, **kw: ({}, {})})
    assert S.generated(PKG, "1010:2000") is real_callee


def test_uninstall_restores_rebound_references(corpus):
    caller, _callee, _dyn, real_callee = corpus
    S.install_overrides(PKG, {"1010:2000": lambda mem, **kw: ({}, {})})
    S.uninstall_overrides(PKG)
    assert caller.func_1010_2000 is real_callee


def test_retro_patch_does_not_clobber_a_delegation_alias(corpus):
    """The over-broad-rebind bug overkill hit, pinned here so the shared seam
    cannot reintroduce it.

    An override that DELEGATES needs a reference to the autolifted body. If the
    retro-patch is keyed only on "holds the original function" it will rebind
    that reference too -- and then the differential compares the override
    against ITSELF and still passes, which is the worst possible outcome for a
    proof mechanism. The rebind must be scoped to the callee's own name.
    """
    caller, _callee, _dyn, real_callee = corpus
    # a port-style oracle alias holding the same function under a DIFFERENT name
    alias = types.ModuleType(f"{PKG}.oracle_alias")
    alias.func_1010_2000__generated = real_callee
    sys.modules[f"{PKG}.oracle_alias"] = alias
    try:
        S.install_overrides(PKG, {"1010:2000": lambda mem, **kw: ({}, {})})
        assert alias.func_1010_2000__generated is real_callee, (
            "retro-patch clobbered the delegation alias -- an override would "
            "then be differentialled against itself and still pass")
        assert S.generated(PKG, "1010:2000") is real_callee
    finally:
        sys.modules.pop(f"{PKG}.oracle_alias", None)
