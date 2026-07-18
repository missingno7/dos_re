"""A switch ARM is an ALTERNATE ENTRY of its container, not a standalone function.

A compiler lowers a dense `switch` into a jump table plus a SHARED EPILOGUE: the container
establishes the frame (`enter`), computes an index and tail-dispatches (`jmp cs:[bx*2+table]`);
each arm runs its case body and falls into the ONE `leave; ret` the container's prologue set up.

A carver that follows only static edges cannot see that.  The arms are unreachable from the
container's entry, so each is carved as its own "function" whose scan re-derives the same shared
tail -- and in isolation that tail is structurally broken: `leave` with no `enter`.  The CPUless
gate refuses it (`leave-without-enter`), which is CORRECT (the arm's frame base genuinely lives in
the container) and also fatal: the container's dynamic-target evidence gate then sees an unpromoted
target and refuses the container too, holding the whole dispatch cluster out of composition.

ABSORPTION (`dos_re.lift.dispatch.absorb_dispatch_arms`) is the graph-completeness repair: union
the arms' instructions back into the container's scan and declare each arm ip a dispatch ALTERNATE
ENTRY.  The container becomes one function with several entry points -- which is what the object
code always was.  Nothing is suppressed: establish and restore now live in the same scan, so the
frame checks pass on their own terms.

DIFFERENTIAL: the absorbed container is composed and its emitted body exec'd, then diffed
byte-for-byte -- whole register file, stack memory, AND virtual time (`_cost`) -- against stepping
the identical container+arm bytes through the interpreter, for EVERY arm of the table.

Soundness guards: an arm whose overlap with the container disagrees byte-for-byte, and an arm that
establishes a frame of its OWN (a genuine tail-called callee, not a shared-epilogue arm), both
REFUSE absorption rather than fuse a body that guesses.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.dispatch import (ArmAbsorptionRefusal, absorb_dispatch_arms,
                                  dispatch_arm_candidates)
from dos_re.lift.emit_cpuless import (Refusal, _contract_inputs,
                                      check_promotable, emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
CS = 0x2000
TABLE_OFF = 0x0020
ARMS = (0x0040, 0x0050)
EPILOGUE = 0x0060
RET_IP = 0x0061

#: The FRAMED dispatch container.
#:   0000 C8 04 00 00      enter 4,0            ; frame + 4 local bytes
#:   0004 8B DE            mov bx, si           ; selector
#:   0006 D1 E3            shl bx, 1
#:   0008 2E FF A7 20 00   jmp cs:[bx+0x0020]   ; TAIL DISPATCH into the table
_CONTAINER = bytes.fromhex("c8040000" "8bde" "d1e3" "2effa72000")

#: The ARMS + the shared epilogue, laid out at their absolute offsets.
#:   0040 B8 11 11         mov ax, 0x1111       ; arm 0
#:   0043 EB 1B            jmp 0x0060
#:   0050 B8 22 22         mov ax, 0x2222       ; arm 1
#:   0053 03 C1            add ax, cx           ; (a longer arm: different cost)
#:   0055 EB 09            jmp 0x0060
#:   0060 C9               leave                ; THE SHARED EPILOGUE
#:   0061 C3               ret
_AT = {
    0x0040: bytes.fromhex("b81111" "eb1b"),
    0x0050: bytes.fromhex("b82222" "03c1" "eb09"),
    0x0060: bytes.fromhex("c9" "c3"),
}


def _code() -> dict[int, int]:
    code = {off: b for off, b in enumerate(_CONTAINER)}
    for base, blob in _AT.items():
        for k, b in enumerate(blob):
            code[base + k] = b
    return code


def _scan(entry: int):
    code = _code()
    return scan_function(lambda off: code.get(off & 0xFFFF, 0x90), entry)


def _seed(mem: Memory) -> None:
    for off, b in _code().items():
        mem.data[(CS << 4) + off] = b
    for n, arm in enumerate(ARMS):
        mem.ww(CS, TABLE_OFF + 2 * n, arm)


# ---------------------------------------------------------------------------
# 1. why absorption is needed: the arm is NOT a function
# ---------------------------------------------------------------------------

def test_standalone_arm_refuses_leave_without_enter():
    """Each arm's scan re-derives the shared epilogue, so in isolation it is a
    `leave` with no `enter` -- correctly refused, and correctly fatal."""
    for arm in ARMS:
        scan = _scan(arm)
        assert EPILOGUE in scan.insts        # it re-derived the shared tail
        with pytest.raises(Refusal) as e:
            check_promotable(scan)
        assert str(e.value) == "leave-without-enter"


def test_container_alone_does_not_contain_its_arms():
    scan = _scan(0)
    for arm in ARMS:
        assert arm not in scan.insts
    ev = {f"{CS:04X}:0008": [f"{CS:04X}:{a:04X}" for a in ARMS]}
    assert dispatch_arm_candidates(scan, CS, ev) == list(ARMS)


# ---------------------------------------------------------------------------
# 2. the seam: absorbed arms are alternate entries of one composable function
# ---------------------------------------------------------------------------

def test_absorbed_container_composes_with_arms_as_alternate_entries():
    merged = absorb_dispatch_arms(_scan(0), {a: _scan(a) for a in ARMS})
    for arm in ARMS:
        assert arm in merged.insts
    assert 0x0000 in merged.insts and EPILOGUE in merged.insts
    # establish and restore now live in the SAME scan: the frame checks pass on
    # their own terms, nothing suppressed.
    spec = check_promotable(merged, dispatch_addrs=frozenset(ARMS))
    assert spec.ret_kind == "near"
    src = emit_recovered(merged, spec.abi, f"{CS:04X}:0000",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         dispatch_addrs=frozenset(ARMS), df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)
    assert "_ENTRIES = {" in src          # exported alternate entries
    for arm in ARMS:
        assert f"0x{arm:04X}" in src      # forced block leaders / _LOCAL landings


# ---------------------------------------------------------------------------
# 3. the DIFFERENTIAL: composed vs interpreter, byte-for-byte + virtual time
# ---------------------------------------------------------------------------

def _run_interpreter(inputs, mem, ss, sp0):
    """Step the real container+arm bytes to the shared epilogue's `ret`,
    returning (the state AT the ret, the instruction count THROUGH the ret).

    The composed body's register/stack contract is the PRE-ret one (the caller
    pops the return frame), while its virtual-time contract COUNTS its own ret
    -- that is the convention a composing caller accumulates."""
    st = CPUState(cs=CS, ip=0, ss=ss, ds=0, es=0,
                  **{r: inputs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = sp0
    cpu = CPU8086(mem, st)
    steps = 0
    for _ in range(256):
        if (cpu.s.cs & 0xFFFF) == CS and (cpu.s.ip & 0xFFFF) == RET_IP:
            break
        cpu.step()
        steps += 1
    else:
        raise AssertionError("container+arm did not reach the shared ret")
    return cpu.s, steps + 1        # + the shared `ret` itself


@pytest.mark.parametrize("selector,expected_ax", [(0, 0x1111), (1, 0x2222 + 0x0007)])
def test_absorbed_dispatch_matches_interpreter_byte_for_byte(selector, expected_ax):
    merged = absorb_dispatch_arms(_scan(0), {a: _scan(a) for a in ARMS})
    spec = check_promotable(merged, dispatch_addrs=frozenset(ARMS))
    src = emit_recovered(merged, spec.abi, f"{CS:04X}:0000",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         dispatch_addrs=frozenset(ARMS), df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)

    ss, sp0 = 0x3000, 0x0100
    inputs = {"ax": 0xABCD, "cx": 0x0007, "bx": 0x0000,
              "si": selector, "di": 0x1234, "bp": 0x5678}
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        _seed(m)
        m.ww(ss, sp0, 0xBEEF)                 # the caller's near return IP

    pkg = types.ModuleType("x")
    pkg.__path__ = []
    sys.modules["x"] = pkg
    dc = types.ModuleType("x._dyncall")

    def _no_dyn(*a, **kw):      # every landing is INTRA-function after absorption
        raise AssertionError("dispatch left the function: absorption failed")

    dc.dyn_exec = _no_dyn
    sys.modules["x._dyncall"] = dc
    ns = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    kw = {r: inputs.get(r, 0) for r in _contract_inputs(merged, spec.abi)
          if r not in ("sp", "ss")}

    class _P:
        pass

    call = dict(mem=m_body, ss=ss, sp=sp0, **kw)
    if spec.needs_plat:
        call["plat"] = _P()
    out, compat = fn(**call)

    s, steps = _run_interpreter(inputs, m_interp, ss, sp0)

    assert s.ax & 0xFFFF == expected_ax, "interpreter reached the wrong arm"
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        got = out[r] if r in out else inputs.get(r, sp0 if r == "sp" else 0)
        assert got & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={got & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == \
        bytes(m_interp.data[base:base + 0x200]), "stack memory diverged"
    # VIRTUAL TIME: the composed body accumulates exactly the instruction count
    # the original executes -- per arm, so a longer arm costs more.
    assert compat["cost"] == steps, (
        f"virtual time: body={compat['cost']} interp={steps}")


# ---------------------------------------------------------------------------
# 4. soundness guards -- a fusion that cannot be PROVEN refuses loudly
# ---------------------------------------------------------------------------

def test_byte_conflicting_overlap_refuses():
    """An "arm" whose bytes disagree with the container at a shared ip is not a
    re-carving of the same instruction stream -- refuse, never merge."""
    arm = _scan(ARMS[0])
    other = _scan(0)
    # forge a disagreement at the container's entry
    arm.insts[0x0000] = type(other.insts[0x0000])(
        **{**vars(other.insts[0x0000]), "raw": b"\x90"})
    with pytest.raises(ArmAbsorptionRefusal) as e:
        absorb_dispatch_arms(other, {ARMS[0]: arm})
    assert str(e.value) == "arm-overlap-byte-conflict"


def test_self_framing_target_refuses():
    """A jump target that establishes its OWN frame is a tail-CALLED function,
    not a shared-epilogue arm: absorbing it would splice a second frame into the
    container's body."""
    code = _code()
    #   0070 55        push bp
    #   0071 8B EC     mov bp, sp
    #   0073 C9        leave
    #   0074 C3        ret
    for k, b in enumerate(bytes.fromhex("55" "8bec" "c9" "c3")):
        code[0x0070 + k] = b
    callee = scan_function(lambda off: code.get(off & 0xFFFF, 0x90), 0x0070)
    with pytest.raises(ArmAbsorptionRefusal) as e:
        absorb_dispatch_arms(_scan(0), {0x0070: callee})
    assert str(e.value) == "arm-establishes-own-frame"
