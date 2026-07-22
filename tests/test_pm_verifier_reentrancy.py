"""PMHookVerifier must not re-enter itself for nested hook calls.

Regression for the first PM graph's fatal crash: a verified hook whose own
execution dispatches other hooks (a linked lifted graph routes each linked
child back through the verifier) recursively re-entered the verifier, and the
verifier clones the WHOLE runtime per call -- nesting O(call-depth) full-runtime
clones, which OOM/stack-crashed a deeply linked graph.  The re-entrancy guard
runs the nested child natively (the ancestor's oracle run already reproduces
the subtree) and verifies each hook exactly once at its outermost occurrence.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.pm_verification import PMHookVerifier, PMHookVerifierConfig


class _FakeCPU:
    def __init__(self):
        self.coverage_telemetry = None
        self.hook_verifier = None


class _RecordingVerifier(PMHookVerifier):
    """PMHookVerifier with the clone+oracle machinery stubbed to a counter --
    isolates the dispatch/guard logic from the (heavy) runtime clone."""

    def __init__(self):
        # bypass __init__'s rt requirement; we never clone here
        self.config = PMHookVerifierConfig()
        self.total_verified = 0
        self.calls_per_hook = {}
        self._active = False
        self.verify_bodies = 0

    def _verify(self, cpu, key, handler, name):
        # Stand-in for the real body: mark active (as the guard expects), run
        # the handler exactly as the real body does at its `handler(cpu)` line,
        # and count.  If the guard were absent, handler()'s nested dispatch
        # would re-enter here and recurse.
        self.verify_bodies += 1
        handler(cpu)
        self.total_verified += 1
        self.calls_per_hook[key] = self.calls_per_hook.get(key, 0) + 1


def test_nested_hook_calls_are_not_reverified():
    cpu = _FakeCPU()
    v = _RecordingVerifier()
    cpu.hook_verifier = v

    # child hook: a leaf
    def child(_cpu):
        pass

    # parent hook: dispatches the child THROUGH the verifier (what call_linked32
    # does while a verifier is active) -- must not trigger a nested _verify.
    def parent(_cpu):
        v(_cpu, 0x2000, child, "child")

    v(cpu, 0x1000, parent, "parent")

    # The parent verified once; the child ran but was NOT separately verified
    # (covered by the parent's oracle), so exactly ONE verify body executed.
    assert v.verify_bodies == 1
    assert v.total_verified == 1
    assert v.calls_per_hook == {0x1000: 1}


def test_deep_nesting_stays_flat():
    """A chain of N linked hooks must run one verify body, not N (no recursion
    blow-up)."""
    cpu = _FakeCPU()
    v = _RecordingVerifier()
    cpu.hook_verifier = v

    def make(depth):
        if depth == 0:
            return lambda _cpu: None
        inner = make(depth - 1)
        return lambda _cpu: v(_cpu, 0x3000 + depth, inner, f"h{depth}")

    v(cpu, 0x3000, make(50), "root")
    assert v.verify_bodies == 1          # not 51
    assert v.total_verified == 1


def test_sequential_top_level_calls_each_verify():
    """The guard only suppresses NESTED calls; independent top-level dispatches
    each verify normally."""
    cpu = _FakeCPU()
    v = _RecordingVerifier()
    cpu.hook_verifier = v
    noop = lambda _cpu: None
    v(cpu, 0x1000, noop, "a")
    v(cpu, 0x1000, noop, "a")
    v(cpu, 0x2000, noop, "b")
    assert v.verify_bodies == 3
    assert v.calls_per_hook == {0x1000: 2, 0x2000: 1}
