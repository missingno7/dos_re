"""The VERIFIER's own regression tests -- synthetic, no game corpus.

Two false greens shipped in a row because the differential had no tests of its
own: it was only ever exercised through a real corpus, where a false green
looks exactly like a pass.  A proof mechanism needs its own proofs, and the
cheapest ones are two toy callables whose disagreement is known in advance.

Each test here corresponds to a defect that reached a commit.
"""
from __future__ import annotations

import pytest

from dos_re.lift.abi_diff import PlatStub, TraceMem, diff_one

_SPIN = "CPUless dispatch spin in 1010:0000 (block 0, cost 0)"

#: minimal proposal: no params, no returns -- isolates the EFFECT comparison
_PROPOSAL = {"params": [], "returns": []}


def _mech(write=None, raise_with=None, plat_port=None):
    def fn(mem, *, _base=0, **kw):
        if write is not None:
            mem.ww(write[0], write[1], write[2])
        if plat_port is not None:
            pass
        if raise_with is not None:
            raise RuntimeError(raise_with)
        return {}, {"flags": 0, "fmask": 0, "cost": 0}
    return fn


def _abi(write=None, raise_with=None):
    def fn(mem, *args, _base=0, **kw):
        if write is not None:
            mem.ww(write[0], write[1], write[2])
        if raise_with is not None:
            raise RuntimeError(raise_with)
        return (), {"flags": 0, "fmask": 0, "cost": 0}
    return fn


def test_matching_raises_still_compare_semantic_writes():
    """A raised state must still compare the effects accumulated BEFORE the
    exception.  Skipping them meant a core that wrote 0xAA and one that wrote
    nothing were 'identical' as long as both raised the same spin error --
    a false green with the effects in plain sight."""
    rep = diff_one(_mech(write=(0x1234, 0x10, 0xAA), raise_with=_SPIN),
                   _abi(raise_with=_SPIN), _PROPOSAL, states=4)
    assert not rep["ok"], "differing writes before a matching raise passed"
    assert any("write" in m.lower() for m in rep["mismatches"])


def test_matching_raises_with_identical_writes_still_pass():
    """The complement: equal effects before an equal raise is a genuine pass,
    so the fix must not turn every raised state into a failure."""
    rep = diff_one(_mech(write=(0x1234, 0x10, 0xAA), raise_with=_SPIN),
                   _abi(write=(0x1234, 0x10, 0xAA), raise_with=_SPIN),
                   _PROPOSAL, states=4)
    assert rep["ok"], rep["mismatches"]
    assert rep["raised"] == 4


def test_a_non_spin_raise_is_not_treated_as_a_spin():
    """Only the emitters' spin marker counts as a spin-wait; any other
    RuntimeError is a real fault."""
    rep = diff_one(_mech(raise_with="guest fault"),
                   _abi(raise_with="guest fault"), _PROPOSAL, states=4)
    assert rep["ok"]
    assert "note" not in rep or "spin" not in rep.get("note", "")


def test_every_requested_state_is_driven():
    """The original defect: three matching raises ended a 64-state run as
    PASSED after three states."""
    rep = diff_one(_mech(raise_with=_SPIN), _abi(raise_with=_SPIN),
                   _PROPOSAL, states=64)
    assert rep["states"] == 64
    assert rep["raised"] == 64


def test_a_divergence_after_the_third_state_is_caught():
    """What the early exit actually hid: state 3+ behaving differently."""
    calls = {"n": 0}

    def mech(mem, *, _base=0, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError(_SPIN)
        mem.ww(0x1234, 0x10, 0xAA)
        return {}, {"flags": 0, "fmask": 0, "cost": 0}

    def abi(mem, *args, _base=0, **kw):
        if calls["n"] <= 3:
            raise RuntimeError(_SPIN)
        return (), {"flags": 0, "fmask": 0, "cost": 0}

    rep = diff_one(mech, abi, _PROPOSAL, states=8)
    assert not rep["ok"]


def test_plat_log_digest_is_deterministic_across_instances():
    """Built-in hash() is randomised per process, so a digest using it is not
    reproducible across runs or parallel workers."""
    a, b = PlatStub(7), PlatStub(7)
    for p in (0x3DA, 0x60, 0x3C8):
        a.inp(p, 1, 0)
        b.inp(p, 1, 0)
    a.intr(0x21, {"ax": 1}, 0)
    b.intr(0x21, {"ax": 1}, 0)
    assert a.log_digest == b.log_digest
    assert a.log_count == b.log_count


def test_trace_mem_digest_survives_the_retained_cap():
    """The cap is for memory, not for evidence: a divergence past the retained
    prefix must still change the digest."""
    a, b = TraceMem(1), TraceMem(1)
    for n in range(TraceMem.MAX_TRACE + 50):
        a.ww(0x100, (n * 2) & 0xFFFF, n & 0xFFFF)
        b.ww(0x100, (n * 2) & 0xFFFF, n & 0xFFFF)
    assert (a.write_digest, a.write_count) == (b.write_digest, b.write_count)
    b.ww(0x100, 0x40, 0xDEAD)
    assert (a.write_digest, a.write_count) != (b.write_digest, b.write_count)
