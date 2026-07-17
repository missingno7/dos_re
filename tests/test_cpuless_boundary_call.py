"""A boundary head may sit on a COMPOSED call, not only a SEQ instruction.

The frame/event loop every DOS game has yields one frame at a `call <frame-
boundary>` site (overkill: 1010:97CB `call 9B2E`).  The CPUless de-carrier used
to refuse any boundary head that wasn't a bare SEQ (`boundary-head-on-transfer`),
even though the VMless emitter already accepts a CALL head.  A boundary head on a
composed near/far call now promotes: the observer (`plat.boundary`) fires AFTER
the recovered callee returns.  A head on an UNCOMPOSED (or otherwise non-call)
transfer still refuses loudly.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import (
    CalleeContract, Refusal, check_promotable, emit_recovered)
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


# call 0x0010 (E8 0D 00) ; ret (C3)  -- one composed near call, then return.
_CODE = bytes.fromhex("E80D00" "C3")
_CALLEE = {0x0010: CalleeContract(name="func_1010_0010", inputs=(),
                                  outputs=("ax",), exit_flags=frozenset())}


def test_boundary_head_on_a_composed_call_promotes():
    scan = _scan(_CODE)
    spec = check_promotable(scan, callees=_CALLEE, boundary_addrs=frozenset({0x0000}))
    assert spec is not None
    assert spec.parks                       # a boundary head makes it standalone-only

    src = emit_recovered(scan, abi_scan(scan, callee_effects={
        0x0010: (frozenset(), frozenset({"ax"}))}), "1010:0100",
        callees=_CALLEE, boundary_addrs=frozenset({0x0000}))
    # the observer fires, and it fires AFTER the composed call (its resume
    # address is the instruction after the call, 0x0003).
    assert "plat.boundary(" in src
    call_at = src.index("func_1010_0010(")
    boundary_at = src.index("plat.boundary(")
    assert call_at < boundary_at          # yield is emitted after the call
    assert "0x0003" in src                # next_ip after the call = resume point


def test_boundary_head_on_an_uncomposed_call_still_refuses():
    scan = _scan(_CODE)
    with pytest.raises(Refusal) as e:
        check_promotable(scan, callees={}, boundary_addrs=frozenset({0x0000}))
    assert "boundary-head-on-transfer" in str(e.value)


def test_boundary_head_on_a_bare_jump_still_refuses():
    # jmp 0x0000 (EB FE) -- a control transfer that is not a call: no
    # "instruction then continue" site, so it is not a valid boundary head.
    scan = _scan(bytes.fromhex("EBFE"))
    with pytest.raises(Refusal) as e:
        check_promotable(scan, boundary_addrs=frozenset({0x0000}))
    assert "boundary-head-on-transfer" in str(e.value)
