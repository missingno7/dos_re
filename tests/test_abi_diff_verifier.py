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
    assert not rep["mismatches"]
    assert rep["raised"] == 4
    # equal effects before an equal raise: nothing DIVERGED, but no return or
    # compat value was ever compared, so this is not verified equivalence
    assert rep["status"] == "inconclusive"


def test_a_non_spin_raise_is_not_treated_as_a_spin():
    """Only the emitters' spin marker counts as a spin-wait; any other
    RuntimeError is a real fault."""
    rep = diff_one(_mech(raise_with="guest fault"),
                   _abi(raise_with="guest fault"), _PROPOSAL, states=4)
    assert rep["spin_states"] == 0, "a plain fault was counted as a spin"
    assert rep["raised"] == 4


def test_matching_real_faults_are_INCONCLUSIVE_not_verified():
    """Equal failure is not proven equivalence.

    Both sides come from shared emitter machinery, so a matching fault can
    mean they share an unsupported behaviour rather than that either is
    correct -- and a core whose every state raised never exercised its
    returns or compat channel at all.  An earlier version reported this
    green, and an earlier version of THIS TEST asserted it should."""
    rep = diff_one(_mech(raise_with="unsupported semantic gap"),
                   _abi(raise_with="unsupported semantic gap"),
                   _PROPOSAL, states=4)
    assert rep["status"] == "inconclusive"
    assert rep["normal_states"] == 0
    assert "INCONCLUSIVE" in rep["note"]


def test_all_states_spinning_is_also_inconclusive():
    """If every state hits the spin cap, nothing positive was established --
    the cap is a frontier, not a proof."""
    rep = diff_one(_mech(raise_with=_SPIN), _abi(raise_with=_SPIN),
                   _PROPOSAL, states=8)
    assert rep["status"] == "inconclusive"


def test_mixed_spin_and_normal_states_stay_INCONCLUSIVE():
    """UNIVERSAL aggregation: the worst state decides.

    Positive evidence for one input does not resolve the inputs that
    established nothing.  An earlier existential rule made one normal match
    plus 63 matching faults a VERIFIED core -- and an earlier version of this
    test asserted exactly that.  Second time a wrong policy was written INTO
    a test, which is the worst place for one: it then defends itself."""
    n = {"i": 0}

    def mech(mem, *, _base=0, **kw):
        n["i"] += 1
        if n["i"] % 2:
            raise RuntimeError(_SPIN)
        return {}, {"flags": 0, "fmask": 0, "cost": 0}

    def abi(mem, *args, _base=0, **kw):
        if n["i"] % 2:
            raise RuntimeError(_SPIN)
        return (), {"flags": 0, "fmask": 0, "cost": 0}

    rep = diff_one(mech, abi, _PROPOSAL, states=8)
    assert rep["status"] == "inconclusive"
    assert rep["normal_states"] > 0, "some states DID compare"
    assert rep["ok"] is False


def test_all_states_normal_is_verified():
    """The only route to verified: every state compared fully."""
    rep = diff_one(_mech(), _abi(), _PROPOSAL, states=8)
    assert rep["status"] == "verified"
    assert rep["ok"] is True
    assert rep["exit_code"] == 0


def test_ok_is_derived_from_the_verdict_not_from_mismatches():
    """`ok` used to be `not mismatches`, so an inconclusive core handed every
    caller of the compatibility field the original false green."""
    rep = diff_one(_mech(raise_with=_SPIN), _abi(raise_with=_SPIN),
                   _PROPOSAL, states=4)
    assert rep["mismatches"] == []
    assert rep["status"] == "inconclusive"
    assert rep["ok"] is False, "empty mismatches must not mean success"
    assert rep["exit_code"] == 2


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


# --- the verdict is DERIVED from the evidence, never stored beside it -------

def test_raise_vs_return_is_a_MISMATCH_not_inconclusive():
    """One side raises, the other returns, but their effects agree.

    The outcome mismatch was recorded, then the state verdict was computed
    from a snapshot taken AFTER it -- so a genuine divergence was classified
    inconclusive (exit 2) instead of mismatch (exit 1).  The snapshot is now
    taken before any comparison for the state, and the verdict is derived in
    one place from it."""
    rep = diff_one(_mech(raise_with=_SPIN), _abi(), _PROPOSAL, states=4)
    assert rep.status == "mismatch"
    assert rep.exit_code == 1
    assert rep.diagnostics, "the outcome divergence must be reported"


def test_ok_status_and_exit_code_cannot_disagree():
    """They are properties of one verdict, not independently stored fields --
    which is how 'diagnostics say mismatch, verdict says inconclusive' became
    representable in the first place."""
    for mech, abi in ((_mech(), _abi()),
                      (_mech(raise_with=_SPIN), _abi(raise_with=_SPIN)),
                      (_mech(raise_with=_SPIN), _abi())):
        rep = diff_one(mech, abi, _PROPOSAL, states=4)
        assert rep.ok is (rep.status == "verified")
        assert rep.exit_code == {"verified": 0, "mismatch": 1,
                                 "inconclusive": 2,
                                 "internal-error": 3}[rep.status]


def test_an_empty_verdict_set_is_inconclusive_not_verified():
    """Nothing compared means nothing proven.  An empty corpus (or a typoed
    --only) previously rode a synthetic VERIFIED baseline to exit 0."""
    from dos_re.lift.abi_diff import INCONCLUSIVE, aggregate
    assert aggregate([]) is INCONCLUSIVE


def test_a_missing_mechanical_param_is_an_internal_error():
    """The harness cannot drive the pair at all -- that says nothing about the
    core, and used to return a bare dict carrying no verdict."""
    rep = diff_one(_mech(), _abi(), {"params": [{"reg": "zz"}], "returns": []},
                   states=2)
    assert rep.status == "internal-error"
    assert rep.exit_code == 3
