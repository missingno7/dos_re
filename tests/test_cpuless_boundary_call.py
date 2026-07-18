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


# cmp byte cs:[0x066B], 0 (2E 80 3E 6B 06 00) ; jz -8 (74 F8) ; ret (C3)
# -- OVERKILL's 1010:0679 tick-wait, the smallest real env-wait shape: it polls
# one memory byte an ISR sets and has NO stack traffic and NO calls at all.
_TICK_WAIT = bytes.fromhex("2E803E6B0600" "74F8" "C3")


def test_boundary_head_forces_sp_into_a_stack_free_function():
    """A head in a function with no stack traffic must still take ``sp``.

    ``_emit_boundary_observer`` hands ``plat.boundary`` the whole ``_DYN_REGS``
    bundle, which includes ``sp``, and merges it back -- a frame driver may run
    the game's own ISRs across the park, and those use the guest stack.  Before
    this, ``_contract_inputs`` dropped ``sp`` from the signature of any function
    without stack traffic, so the emitted body referenced a name that did not
    exist: the first arrival at the head raised ``UnboundLocalError`` instead of
    yielding.  The only head declared in any port until now happened to sit in a
    function that had calls in it, which is why nothing caught this.
    """
    scan = _scan(_TICK_WAIT)
    heads = frozenset({0x0000})
    spec = check_promotable(scan, boundary_addrs=heads)
    assert spec.parks and spec.needs_plat
    src = emit_recovered(scan, spec.abi, "1010:0679", boundary_addrs=heads,
                         needs_plat=spec.needs_plat,
                         flags_livein=spec.flags_livein)
    assert "plat.boundary(" in src

    ns: dict = {}
    exec(compile(src, "gen", "exec"), ns)          # noqa: S102 -- generated code
    fn = ns["func_1010_0679"]
    import inspect
    assert "sp" in inspect.signature(fn).parameters

    class _Mem:
        def rb(self, _cs, _off):
            return self.v
        v = 0

    class _Plat:
        calls = 0

        def boundary(self, _cs, _ip, _rip, regs, _cost):
            self.calls += 1
            mem.v = 1                              # the "ISR" the park delivers
            return regs, regs["_flags_in"], 0

    mem, plat = _Mem(), _Plat()
    fn(mem, plat, sp=0x1234, ss=0x5678)            # must not raise
    # TWO arrivals, and that is the shape a frame driver must be built for: the
    # observer fires AFTER the poll instruction, so the state the park changed
    # is only observed on the NEXT trip round the wait.  Pass 1 still exits on
    # the stale compare; pass 2 sees the tick and leaves.  (This is why a driver
    # parks on RE-arrival: parking on pass 1 would cut the frame before the wait
    # body reached steady state.)
    assert plat.calls == 2
