"""Fail-loud stub for a runtime-dead near call (the CPUless hard wall).

For a STANDALONE CPUless corpus (`cpuless_promote --observed`), a near call to
a target that is neither an IR function nor ever executed is runtime-dead: a
never-taken branch, or a census gap in an untested code path.  Rather than let
that dead call block a runtime-reached CALLER from promoting, the promoter
models it as an empty-effect, stack-balanced synthetic callee (so composition
stays sound on every LIVE path) and passes its target in ``stub_targets``.  The
emitter then turns the call SITE into a `raise` -- if the dead path is ever
reached at runtime, it fails loud instead of silently falling through.  A real
(non-stub) call still composes into the normal recovered call machinery.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import (CalleeContract, Refusal,
                                      check_promotable, emit_recovered)
from dos_re.lift.cpuless import abi_scan


def _scan(code: bytes) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90  # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        if i.kind in ("ret", "retf", "iret"):
            s.exits.append(i)
            break
        ip = i.next_ip
    return s


def _scan_all(code: bytes) -> FunctionScan:
    """Decode every byte, marking each ret/retf/iret an exit (multi-exit)."""
    fetch = lambda o: code[o] if o < len(code) else 0x90  # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        if i.kind in ("ret", "retf", "iret"):
            s.exits.append(i)
        ip = i.next_ip
    return s


# call 0x0010 (E8 0D 00) ; ret (C3)
_CODE = bytes.fromhex("E80D00" "C3")
# the empty-effect, stack-balanced synthetic the promoter injects for a
# runtime-dead target (mirrors cpuless_promote._STUB).
_STUB = CalleeContract(
    name="<unrecovered>", inputs=(), outputs=(),
    exit_flags=frozenset({"cf", "pf", "af", "zf", "sf", "of", "df", "intf"}),
    ret_kind="near", sp_delta=0, ret_pop=0, sp_output=False, sp_deltas=(0,))


def _abi(scan):
    return abi_scan(scan, callee_effects={0x0010: (frozenset(), frozenset())})


def test_dead_call_becomes_a_fail_loud_raise() -> None:
    scan = _scan(_CODE)
    src = emit_recovered(scan, _abi(scan), "1010:0100",
                         callees={0x0010: _STUB},
                         stub_targets=frozenset({0x0010}))
    # the call site is a raise, not a call into the (nonexistent) recovered fn.
    assert "raise RuntimeError('CPUless: unrecovered call to 1010:0010" in src
    assert "func_1010_0010(" not in src
    # the raise must compile.
    compile(src, "<stub>", "exec")


def test_a_real_call_still_composes_when_not_stubbed() -> None:
    # SAME function, empty stub_targets: the call composes into the normal
    # recovered call machinery (a call into the promoted callee, no raise).
    scan = _scan(_CODE)
    src = emit_recovered(scan, _abi(scan), "1010:0100",
                         callees={0x0010: CalleeContract(
                             name="func_1010_0010", inputs=(), outputs=(),
                             exit_flags=frozenset())},
                         stub_targets=frozenset())
    assert "func_1010_0010(" in src
    assert "unrecovered call" not in src


# --- runtime-dead EXITS (symmetric to the dead-call stub) --------------------
# jz 0003 ; ret (near) ; retf (far).  Two exits of DIFFERENT return kinds.
_MIXED = bytes.fromhex("7401" "c3" "cb")   # 0000 jcc, 0002 ret, 0003 retf


def test_mixed_return_kinds_refuses_without_dead_exit_evidence() -> None:
    # both exits live -> a near ret and a far retf in one function: the exit
    # ABI is ambiguous, refuse.
    with pytest.raises(Refusal, match="mixed-return-kinds"):
        check_promotable(_scan_all(_MIXED))


def test_a_runtime_dead_exit_no_longer_constrains_the_exit_abi() -> None:
    # the retf at 0003 is runtime-dead -> only the live near ret constrains the
    # ABI, so the function promotes (ret_kind near) instead of refusing.
    scan = _scan_all(_MIXED)
    spec = check_promotable(scan, dead_exits=frozenset({0x0003}))
    assert spec.ret_kind == "near"
    src = emit_recovered(scan, spec.abi, "1010:2000",
                         dead_exits=frozenset({0x0003}))
    # the dead retf becomes a fail-loud raise; the live ret stays a normal exit.
    assert "raise RuntimeError('CPUless: runtime-dead exit at 1010:0003" in src
    compile(src, "<dead-exit>", "exec")
