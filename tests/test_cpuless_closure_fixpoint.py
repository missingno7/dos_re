"""The capture<->close composability fixpoint (composable_closure).

Reachability alone does not say how much of the reached set is CPUless-
COMPOSABLE. That is a second, coupled fixpoint over the callee graph -- a caller
composes only when every callee it reaches composes -- and it has two properties
a naive "compose when all callees already compose" pass gets wrong:

  * a dispatch target is reached ONLY by following the observed evidence
    captured at the caller's indirect site (the differential below: without the
    evidence the target is not even in the closure);
  * a mutually-recursive dispatch cluster is a strongly-connected component, so
    no member bottoms out first -- it must promote ATOMICALLY once every edge
    leaving the component lands on a composable target.

These generalise beyond any one game, so they live in dos_re.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from cpuless_closure import composable_closure  # noqa: E402


def _seq(ip, bytes_="b80000"):
    return {"ip": ip, "bytes": bytes_, "mnemonic": "mov r16,imm16", "kind": "seq"}


def _call(ip, target):
    return {"ip": ip, "bytes": "e80000", "mnemonic": "call", "kind": "call",
            "target": target}


def _jmp_ind(ip):
    return {"ip": ip, "bytes": "ffe0", "mnemonic": "jmp rm16", "kind": "jmp_ind",
            "mem_operand": True}


def _fn(insts):
    return {"blocks": [{"instructions": insts}]}


# A and D are a message-pump-style dispatch cluster: each near-calls the leaf L
# (1000:0500) and then TAIL-DISPATCHES (jmp_ind) to the other -- observed only at
# runtime. A also dispatches to the leaf C (1000:0300). E near-calls X, an
# x87-blocked body. P is an already-promoted function; 1000:0803 is a resume
# address inside its byte span.
_IR = {
    "functions": {
        "1000:0100": _fn([_call("0100", "0500"), _jmp_ind("0106")]),   # A
        "1000:0400": _fn([_call("0400", "0500"), _jmp_ind("0406")]),   # D
        "1000:0300": _fn([_seq("0300")]),                              # C (leaf)
        "1000:0500": _fn([_seq("0500")]),                              # L (leaf)
        "1000:0600": _fn([_seq("0600")]),                              # X (x87)
        "1000:0700": _fn([_call("0700", "0600")]),                     # E -> X
        "1000:0800": _fn([_seq("0800"), _seq("0803")]),                # P (promoted)
    }
}
# A's dispatch resolves (at runtime) to D and C; D's dispatch resolves to A.
_DYN = {
    "1000:0106": ["1000:0400", "1000:0300"],
    "1000:0406": ["1000:0100"],
}
_BODY_CLEAN = {"1000:0100", "1000:0400", "1000:0300", "1000:0500", "1000:0700"}
_REFUSALS = {"1000:0600": "x87-fpu"}


def test_atomic_scc_and_evidence_and_blocked_callee() -> None:
    rep = composable_closure(
        _IR, ["1000:0100", "1000:0700"],
        promoted=set(), body_clean=_BODY_CLEAN, dyn_evidence=_DYN,
        refusals=_REFUSALS)

    # the evidence made D and C reachable (they are reached ONLY through A's
    # observed tail-dispatch).
    assert rep["reached"] == 6                       # A D C L X E
    comp = set(rep["composable_keys"])

    # the cyclic cluster {A, D} promoted ATOMICALLY (a non-atomic fixpoint would
    # deadlock: A needs D, D needs A).
    assert {"1000:0100", "1000:0400"} <= comp
    assert rep["max_scc_size"] == 2
    assert ["1000:0100", "1000:0400"] in rep["cyclic_components"]

    # the leaves compose; the x87 body does not, and E is blocked BY it.
    assert {"1000:0300", "1000:0500"} <= comp
    assert "1000:0600" not in comp and "1000:0700" not in comp
    assert rep["frontier"]["1000:0600"] == "x87-fpu"
    assert rep["frontier"]["1000:0700"] == "blocked-by-callee:1000:0600"
    assert rep["composable"] == 4                    # A D C L
    assert rep["converged"] is True


def test_without_evidence_the_dispatch_targets_are_not_in_the_closure() -> None:
    # The differential: drop the captured evidence and the tail-dispatch edges
    # vanish -- D and C are never reached, so the closure silently UNDER-measures.
    rep = composable_closure(
        _IR, ["1000:0100", "1000:0700"],
        promoted=set(), body_clean=_BODY_CLEAN, dyn_evidence={},
        refusals=_REFUSALS)
    keys = set(rep["composable_keys"])
    assert "1000:0400" not in keys                   # D unreached
    assert "1000:0300" not in keys                   # C unreached
    assert rep["reached"] == 4                        # A L X E only


def test_resume_address_inside_a_promoted_function_composes() -> None:
    # 1000:0803 is an offset inside the promoted P (1000:0800); its recovered
    # body serves the resume, so it is covered, not a frontier gap.
    rep = composable_closure(
        _IR, ["1000:0800", "1000:0803"],
        promoted={"1000:0800"}, body_clean=set(), dyn_evidence={})
    assert "1000:0803" not in rep["frontier"]
    assert rep["frontier"] == {}
    assert rep["closure_complete"] is True


def test_static_only_split_when_observed_is_supplied() -> None:
    # X is x87-blocked; if the run never executed it, it is reported as
    # static-only, not a runtime blocker -- the honest completion target.
    rep = composable_closure(
        _IR, ["1000:0100", "1000:0700"],
        promoted=set(), body_clean=_BODY_CLEAN, dyn_evidence=_DYN,
        refusals=_REFUSALS,
        observed=["1000:0100", "1000:0400", "1000:0300", "1000:0500",
                  "1000:0700"])           # X (0600) NOT executed
    assert "1000:0600" in rep["static_only_frontier"]
    assert "1000:0600" not in rep["runtime_frontier"]
    # E executed but is blocked by the (unexecuted) X -> still a runtime item.
    assert "1000:0700" in rep["runtime_frontier"]
