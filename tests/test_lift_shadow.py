"""The shadow rung: what it must compare, and what it must refuse to call a pass.

A shadow's job is to be UNABLE to produce a false green, so most of these tests
assert that something fails. The three that matter:

* a candidate agreeing on AX while differing anywhere else is a MISMATCH -- the
  previous checker-callback design compared AX alone and was indistinguishable
  from a total one;
* an exemption with no written reason is an error, so a hole in the proof cannot
  be opened silently;
* zero calls is INCONCLUSIVE, never VERIFIED.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.lift import standalone as S
from dos_re.lift.shadow import (Exemption, Verdict, install_shadows, record_for,
                                report, reset, verdict)

PKG = "shadow_corpus"


def _gen(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
    """A generated-shaped body: seven-ish outputs, a compat channel, and it WRITES."""
    mem.ww(ss, (sp - 2) & 0xFFFF, bx)
    return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
            {"flags": 0x41, "fmask": 0x8D5, "cost": 104})


@pytest.fixture
def corpus():
    mod = types.ModuleType(f"{PKG}.func_1010_04c0")
    mod.func_1010_04c0 = _gen
    pkg = types.ModuleType(PKG)
    sys.modules[PKG] = pkg
    sys.modules[f"{PKG}.func_1010_04c0"] = mod
    reset()
    yield mod
    S.uninstall_overrides(PKG)
    reset()
    sys.modules.pop(PKG, None)
    sys.modules.pop(f"{PKG}.func_1010_04c0", None)


class Mem:
    def __init__(self):
        self.data = bytearray(0x20000)

    def rb(self, seg, off):
        return self.data[(((seg & 0xFFFF) << 4) + (off & 0xFFFF)) % len(self.data)]

    def rw(self, seg, off):
        return self.rb(seg, off) | (self.rb(seg, (off + 1) & 0xFFFF) << 8)

    def wb(self, seg, off, val):
        self.data[(((seg & 0xFFFF) << 4) + (off & 0xFFFF)) % len(self.data)] = val & 0xFF

    def ww(self, seg, off, val):
        self.wb(seg, off, val & 0xFF)
        self.wb(seg, (off + 1) & 0xFFFF, (val >> 8) & 0xFF)


def _call(kw=None):
    from shadow_corpus.func_1010_04c0 import func_1010_04c0
    mem = Mem()
    return mem, func_1010_04c0(mem, **(kw or {"ax": 5, "bx": 0x1234, "ss": 0x100, "sp": 0x40}))


def test_an_exact_candidate_verifies_and_the_generated_body_still_drives(corpus):
    install_shadows(PKG, {"1010:04C0": _gen})
    mem, (out, compat) = _call()
    assert (out, compat) == ({"ax": 6, "bx": 0x1234, "cx": 0x80},
                             {"flags": 0x41, "fmask": 0x8D5, "cost": 104})
    assert mem.rw(0x100, 0x3E) == 0x1234        # the generated write landed for real
    assert verdict() is Verdict.VERIFIED
    assert record_for("1010:04C0").calls == 1
    assert record_for("1010:04C0").costs == {104: 1}


@pytest.mark.parametrize("mutate, needle", [
    (lambda o, c: o.__setitem__("bx", 0), "output bx differs"),
    (lambda o, c: o.__setitem__("cx", 0x81), "output cx differs"),
    (lambda o, c: o.pop("cx"), "omits output"),
    (lambda o, c: c.__setitem__("flags", 0x40), "compat flags differs"),
    (lambda o, c: c.__setitem__("fmask", 0x8D4), "compat fmask differs"),
    (lambda o, c: c.__setitem__("cost", 19), "compat cost differs"),
])
def test_agreeing_on_ax_alone_is_not_enough(corpus, mutate, needle):
    """The whole contract is compared, not the one output a checker chose to look at."""
    def candidate(mem, **kw):
        o, c = _gen(mem, **kw)
        mutate(o, c)
        return o, c

    install_shadows(PKG, {"1010:04C0": candidate})
    with pytest.raises(AssertionError, match=needle):
        _call()
    assert verdict() is Verdict.MISMATCH
    assert record_for("1010:04C0").calls == 0


def test_a_candidate_that_skips_the_stack_write_is_a_mismatch(corpus):
    """AX, BX, CX, flags, fmask and cost all agree; only the memory residue differs."""
    def candidate(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
        return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
                {"flags": 0x41, "fmask": 0x8D5, "cost": 104})

    install_shadows(PKG, {"1010:04C0": candidate})
    with pytest.raises(AssertionError, match="memory write COUNT differs"):
        _call()
    assert verdict() is Verdict.MISMATCH


def test_a_wrong_stack_write_value_is_a_mismatch(corpus):
    def candidate(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
        mem.ww(ss, (sp - 2) & 0xFFFF, bx ^ 0xFF)
        return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
                {"flags": 0x41, "fmask": 0x8D5, "cost": 104})

    install_shadows(PKG, {"1010:04C0": candidate})
    with pytest.raises(AssertionError, match="memory write #0 differs"):
        _call()


def test_the_candidate_cannot_perturb_the_run(corpus):
    """Its writes go to an overlay; the machine only ever sees the generated body."""
    def candidate(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
        mem.ww(ss, (sp - 2) & 0xFFFF, bx)
        mem.ww(ss, 0x900, 0xDEAD)                    # ... and one the generated body never makes
        return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
                {"flags": 0x41, "fmask": 0x8D5, "cost": 104})

    install_shadows(PKG, {"1010:04C0": candidate}, fail_fast=False)
    mem, _ = _call()
    assert mem.rw(0x100, 0x900) == 0                 # never reached the machine
    assert verdict() is Verdict.MISMATCH


def test_the_candidate_reads_its_own_writes_and_the_pre_state(corpus):
    """Read-your-writes through the overlay, and reads see the machine BEFORE the driver."""
    seen = {}

    def candidate(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
        mem.ww(ss, (sp - 2) & 0xFFFF, bx)
        seen["own"] = mem.rw(ss, (sp - 2) & 0xFFFF)
        seen["pre"] = mem.rw(ss, 0x900)
        return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
                {"flags": 0x41, "fmask": 0x8D5, "cost": 104})

    install_shadows(PKG, {"1010:04C0": candidate})
    _call()
    assert seen["own"] == 0x1234
    assert seen["pre"] == 0
    assert verdict() is Verdict.VERIFIED


def test_zero_calls_is_inconclusive_not_verified(corpus):
    """An override that was never exercised established NOTHING. This is the
    false-green class that has bitten this ecosystem twice."""
    install_shadows(PKG, {"1010:04C0": _gen})
    assert record_for("1010:04C0").calls == 0
    assert verdict() is Verdict.INCONCLUSIVE
    assert "NEVER CALLED" in report()


def test_no_shadow_installed_is_inconclusive():
    reset()
    assert verdict() is Verdict.INCONCLUSIVE
    assert "nothing was established" in report()


def test_a_mismatch_outranks_an_inconclusive_shadow(corpus):
    """min() over the worst-first lattice: one bad shadow cannot be outvoted."""
    def bad(mem, **kw):
        o, c = _gen(mem, **kw)
        o["ax"] = 0
        return o, c

    other = types.ModuleType(f"{PKG}.func_1010_3a96")
    other.func_1010_3a96 = _gen
    sys.modules[f"{PKG}.func_1010_3a96"] = other
    try:
        install_shadows(PKG, {"1010:04C0": bad, "1010:3A96": _gen}, fail_fast=False)
        _call()
        assert record_for("1010:3A96").verdict is Verdict.INCONCLUSIVE
        assert record_for("1010:04C0").verdict is Verdict.MISMATCH
        assert verdict() is Verdict.MISMATCH
    finally:
        sys.modules.pop(f"{PKG}.func_1010_3a96", None)


def test_an_exemption_without_a_reason_is_refused():
    with pytest.raises(ValueError, match="written reason"):
        Exemption(outputs={"di"})
    with pytest.raises(ValueError, match="written reason"):
        Exemption(memory=True)
    with pytest.raises(ValueError, match="written reason"):
        Exemption(compat={"cost"}, reason="   ")
    assert Exemption().reason == ""                  # the empty, total exemption is fine


def test_an_exemption_naming_an_unknown_compat_key_is_refused():
    with pytest.raises(ValueError, match="unknown compat key"):
        Exemption(compat={"cycles"}, reason="typo for cost")


def test_an_exemption_for_an_unshadowed_address_is_refused(corpus):
    with pytest.raises(ValueError, match="un-shadowed address"):
        install_shadows(PKG, {"1010:04C0": _gen},
                        exemptions={"1010:9999": Exemption(memory=True, reason="x")})


def test_a_justified_exemption_narrows_the_comparison_and_is_reported(corpus):
    def candidate(mem, *, ax=0, bx=0, ss=0, sp=0, **kw):
        return ({"ax": (ax + 1) & 0xFFFF, "bx": bx, "cx": 0x80},
                {"flags": 0x41, "fmask": 0x8D5, "cost": 104})

    install_shadows(PKG, {"1010:04C0": candidate}, exemptions={
        "1010:04C0": Exemption(memory=True,
                               reason="candidate keeps the frame in Python locals")})
    _call()
    assert verdict() is Verdict.VERIFIED
    assert "[exempt: memory]" in report()


def test_a_raising_candidate_is_a_mismatch_not_an_internal_error(corpus):
    """The generated body returns; the candidate does not. That DISPROVES it."""
    def candidate(mem, **kw):
        raise ZeroDivisionError("div by zero (recovered)")

    install_shadows(PKG, {"1010:04C0": candidate}, fail_fast=False)
    _call()
    assert verdict() is Verdict.MISMATCH
    assert "candidate raised ZeroDivisionError" in report()


def test_fail_fast_raises_at_the_call_but_still_records(corpus):
    def bad(mem, **kw):
        o, c = _gen(mem, **kw)
        o["ax"] = 0
        return o, c

    install_shadows(PKG, {"1010:04C0": bad})
    with pytest.raises(AssertionError, match="output ax differs"):
        _call()
    assert verdict() is Verdict.MISMATCH
