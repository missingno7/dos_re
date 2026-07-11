"""Tests for dos_re.coverage — the measured native-% collector.

Game-free: a toy classifier over synthetic addresses; events fed directly (the CPU/verifier only call
these four methods)."""
from __future__ import annotations

from dos_re.coverage import CoverageCollector, fmt_addr


def classify(addr, name=""):
    if "decode" in name:
        return "codecs"
    return "gameplay" if addr[1] < 0x8000 else "render"


A_GAME = (0x1030, 0x1000)
A_RENDER = (0x1030, 0x9000)
H_DECODE = (0x1030, 0x4537)


def test_native_percent_measured():
    cov = CoverageCollector(classifier=classify)
    for _ in range(300):
        cov.record_interpreted_instruction(A_GAME)
    for _ in range(100):
        cov.record_interpreted_instruction(A_RENDER)
    cov.record_hook_verified(H_DECODE, "decode_4537", 600)     # one call replaced 600 ASM instructions
    # native % = 600 / (400 interpreted + 600 equiv) = 60%
    assert abs(cov.native_percent() - 60.0) < 1e-9
    s = cov.snapshot()
    assert s["hook_equiv_measured"] == 600 and s["hook_equiv_estimated"] == 0
    assert s["islands"]["codecs"]["hook_equiv"] == 600
    assert s["islands"]["gameplay"]["interpreted"] == 300
    assert s["islands"]["render"]["interpreted"] == 100
    assert "native %" in cov.format_summary()


def test_bounded_original_is_not_remaining_frontier():
    cov = CoverageCollector(classifier=classify)
    cov.record_interpreted_instruction(A_GAME)
    with cov.bounded_original():                                # an oracle reference run
        for _ in range(50):
            cov.record_interpreted_instruction(A_GAME)
    assert cov.total_interpreted == 1 and cov.total_bounded == 50
    assert cov.snapshot()["islands"]["gameplay"]["bounded"] == 50
    # bounded work is in the denominator (measured) but not the interpreted frontier
    cov.record_hook_verified(H_DECODE, "decode_4537", 51)
    assert abs(cov.native_percent() - 50.0) < 1e-9              # 51 / (1 + 50 + 51)


def test_unverified_estimates_from_own_run_then_unmeasured():
    cov = CoverageCollector(classifier=classify)
    cov.record_hook_verified(H_DECODE, "decode_4537", 100)      # establishes avg=100 within this run
    cov.record_hook_unverified(H_DECODE, "decode_4537")         # estimated at the run's own average
    st = cov.hooks[H_DECODE]
    assert st.estimated_equiv == 100.0 and st.unmeasured_calls == 0
    # a hook with NO measurement anywhere counts as unmeasured — outside the percentage
    other = (0x1030, 0x5000)
    cov.record_hook_unverified(other, "mystery")
    assert cov.hooks[other].unmeasured_calls == 1
    s = cov.snapshot()
    assert s["unmeasured_hook_calls"] == 1
    assert "UNMEASURED" in cov.format_summary()


def test_cache_round_trip_enables_estimate_mode(tmp_path):
    cache = tmp_path / "coverage_cache.json"
    run1 = CoverageCollector(classifier=classify, cache_path=cache)
    for _ in range(4):
        run1.record_hook_verified(H_DECODE, "decode_4537", 250)
    run1.save_cache()
    assert cache.exists()
    # a later run WITHOUT the verifier: unverified calls estimate from the cached average
    run2 = CoverageCollector(classifier=classify, cache_path=cache)
    run2.record_hook_unverified(H_DECODE, "decode_4537")
    st = run2.hooks[H_DECODE]
    assert st.estimated_equiv == 250.0 and st.unmeasured_calls == 0


def test_skipped_and_default_classifier_and_disabled():
    cov = CoverageCollector()                                    # no classifier -> everything "unknown"
    cov.record_hook_skipped(H_DECODE, "decode_4537")
    assert cov.hooks[H_DECODE].skipped == 1 and cov.hooks[H_DECODE].island == "unknown"
    cov.record_interpreted_instruction(A_GAME)
    assert cov.snapshot()["islands"]["unknown"]["interpreted"] == 1
    off = CoverageCollector(enabled=False)
    off.record_interpreted_instruction(A_GAME)
    off.record_hook_verified(H_DECODE, "x", 10)
    assert off.total_interpreted == 0 and not off.hooks


def test_one_arg_classifier_supported():
    cov = CoverageCollector(classifier=lambda addr: "flat")
    cov.record_interpreted_instruction(A_GAME)
    cov.record_hook_verified(H_DECODE, "n", 5)
    assert set(cov.snapshot()["islands"]) == {"flat"}


def test_fmt_addr():
    assert fmt_addr((0x1030, 0x4537)) == "1030:4537"
