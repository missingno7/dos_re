"""The ss address-space split: stack, data-in-ss, and genuinely ambiguous.

The negative tests are the point of this file.  Widening the accepted set is
the whole risk of this mechanism -- an ss classifier that is too generous
hands the emitter a "data" access that can reach the live frame, and the
resulting core is wrong in a way the static wall cannot see.  So every test
that ACCEPTS something is paired with one that must still refuse.
"""
from __future__ import annotations

import pytest

from dos_re.lift.addrspace import (Space, classify_ss, has_frame_access,
                                   ss_data_verdict)
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one

FLOOR = 0x200


def _scan(code: bytes) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90    # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        ip = i.next_ip
    return s


def _spaces(code: bytes, floor=FLOOR):
    return [a.space for a in classify_ss(_scan(code), stack_floor=floor)]


# --------------------------------------------------------------------------
# 1. machine stack -- the part de-stacking deletes

@pytest.mark.parametrize("code,what", [
    (b"\x50", "push ax"), (b"\x58", "pop ax"),
    (b"\x9C", "pushf"), (b"\x9D", "popf"),
    (b"\xE8\x00\x00", "call near"), (b"\xC3", "ret"),
    (b"\xCB", "retf"), (b"\xCF", "iret"),
    (b"\x1E", "push ds"), (b"\x1F", "pop ds"),
])
def test_implicit_stack_traffic_is_machine_stack(code, what):
    assert _spaces(code) == [Space.MACHINE_STACK], what


@pytest.mark.parametrize("code,what", [
    (b"\x8B\x46\x04", "mov ax,[bp+4]"),
    (b"\x8B\x42\x00", "mov ax,[bp+si]"),
    (b"\x8B\x43\x00", "mov ax,[bp+di]"),
])
def test_bp_relative_frame_slots_are_machine_stack(code, what):
    """bp holds a caller-established frame pointer: these are real stack
    accesses even though no prefix says so."""
    assert _spaces(code) == [Space.MACHINE_STACK], what
    assert has_frame_access(classify_ss(_scan(code), stack_floor=FLOOR))


def test_disp16_is_not_a_frame_slot():
    """mod=0 rm=6 encodes [disp16] and defaults to DS -- it only LOOKS like
    the bp form because they share an rm value."""
    assert _spaces(b"\x8B\x1E\x49\xA9") == []


# --------------------------------------------------------------------------
# 2. data that merely lives in the stack segment

def test_a_constant_ss_offset_below_the_floor_is_data():
    # mov ax, ss:[0x0006]
    assert _spaces(b"\x36\xA1\x06\x00") == [Space.SS_DATA]


def test_a_constant_ss_offset_above_the_floor_is_ambiguous():
    """This is the case the floor exists to catch: a constant that reaches
    into the live stack is NOT data just because it is constant."""
    assert _spaces(b"\x36\xA1\x00\x30") == [Space.AMBIGUOUS]


def test_an_access_straddling_the_floor_is_ambiguous():
    """Width matters: a 2-byte read at floor-1 spans floor-1..floor, so its
    high byte is already machine stack."""
    code = b"\x36\xA1" + (FLOOR - 1).to_bytes(2, "little")
    assert _spaces(code) == [Space.AMBIGUOUS]
    code_ok = b"\x36\xA1" + (FLOOR - 2).to_bytes(2, "little")
    assert _spaces(code_ok) == [Space.SS_DATA]


def test_without_a_floor_nothing_constant_is_provable():
    """The floor is a per-program layout fact.  With no floor supplied there
    is no boundary, so no constant can be shown to be data."""
    assert _spaces(b"\x36\xA1\x06\x00", floor=None) == [Space.AMBIGUOUS]


# --------------------------------------------------------------------------
# 3. computed ss addresses -- unproven until evidence arrives

def test_a_computed_ss_address_is_unproven_not_data():
    """`mov al, ss:[bx]` is exactly 1010:1F19's shape.  It may address the
    terrain map; it may address the live frame.  The classifier does not
    guess, and it does not refuse outright either -- it records a proof
    obligation."""
    assert _spaces(b"\x36\x8A\x07") == [Space.SS_DATA_UNPROVEN]


def test_a_computed_ss_address_stays_refused_without_evidence():
    """THE load-bearing negative test.  A computed ss address can reach the
    live stack, so it must not become a data access merely because someone
    wants the function verified."""
    acc = classify_ss(_scan(b"\x36\x8A\x07"), stack_floor=FLOOR)
    ok, reason = ss_data_verdict(acc)
    assert not ok
    assert reason == "computed-ss-address-unproven"


def test_evidence_upgrades_a_computed_access_to_data():
    acc = classify_ss(_scan(b"\x36\x8A\x07"), stack_floor=FLOOR)
    ok, reason = ss_data_verdict(acc, sp_independence_proven=True)
    assert ok, reason


def test_evidence_does_NOT_rescue_an_ambiguous_constant():
    """sp-independence evidence says writes do not move with sp.  It says
    nothing about a constant that provably lands inside the live stack, so it
    must not launder one."""
    acc = classify_ss(_scan(b"\x36\xA1\x00\x30"), stack_floor=FLOOR)
    ok, reason = ss_data_verdict(acc, sp_independence_proven=True)
    assert not ok
    assert reason.startswith("ambiguous-ss-address")


def test_evidence_does_not_invent_data_where_there_is_none():
    """A function with only push/pop has no ss data, and no amount of
    evidence should make ss a data-segment input for it."""
    acc = classify_ss(_scan(b"\x50\x58\xC3"), stack_floor=FLOOR)
    ok, reason = ss_data_verdict(acc, sp_independence_proven=True)
    assert not ok
    assert reason == "ss is machine stack only"


# --------------------------------------------------------------------------
# 4. the mixed function -- one segment, two live uses

def test_data_and_frame_together_are_reported_not_merged():
    """A function may read its own frame AND use ss for data.  De-stacking
    removes the frame, so this is not disqualifying -- but the caller must be
    able to see it, because the differential then has to separate two live
    uses of one segment."""
    code = b"\x36\xA1\x06\x00" + b"\x8B\x46\x04"     # ss:[6] data, [bp+4] frame
    acc = classify_ss(_scan(code), stack_floor=FLOOR)
    spaces = [a.space for a in acc]
    assert Space.SS_DATA in spaces and Space.MACHINE_STACK in spaces
    assert has_frame_access(acc)
    ok, _ = ss_data_verdict(acc)
    assert ok, "the frame slot does not disqualify the data use"


def test_a_frame_slot_alone_never_makes_ss_a_data_segment():
    acc = classify_ss(_scan(b"\x8B\x46\x04"), stack_floor=FLOOR)
    ok, reason = ss_data_verdict(acc)
    assert not ok and reason == "ss is machine stack only"


# --------------------------------------------------------------------------
# 5. overrides to OTHER segments must not be read as ss

@pytest.mark.parametrize("pfx,name", [(b"\x26", "es"), (b"\x2E", "cs"),
                                      (b"\x3E", "ds")])
def test_other_segment_overrides_are_not_ss_accesses(pfx, name):
    assert _spaces(pfx + b"\x8A\x07") == [], name


def test_an_es_override_on_a_bp_form_is_not_a_frame_slot():
    """`es:[bp+4]` redirects away from ss; it is not a stack access."""
    assert _spaces(b"\x26\x8B\x46\x04") == []


# --------------------------------------------------------------------------
# 6. the emitter gate: composable SHAPE is not a discharged OBLIGATION

def test_check_composable_refuses_computed_ss_by_default():
    """The whole mechanism must not widen the corpus on its own.

    A computed ss access is structurally composable, but the evidence that
    it is sp-independent does not exist yet.  Admitting it now would hand the
    differential an "ss data" access that may be a frame slot -- and the
    differential EXCLUDES stack writes from comparison, so the error would be
    excluded rather than caught.  Default must refuse.
    """
    from dos_re.lift.emit_abi import check_composable, Refusal
    # push/pop makes this a REAL stack user.  Without stack traffic
    # ss_is_data_segment already treats ss as a pure selector and this gate
    # never runs -- a fixture without it tests nothing.
    scan = _scan(b"\x50\x36\x8A\x07\x58\xC3")   # push ax; mov al,ss:[bx]; pop; ret
    with pytest.raises(Refusal) as e:
        check_composable(scan, ss_globals_floor=FLOOR)
    assert "computed-ss-address-unproven" in str(e.value)


def test_check_composable_admits_it_only_under_explicit_opt_in():
    from dos_re.lift.emit_abi import check_composable
    scan = _scan(b"\x36\x8A\x07\xC3")
    depth = check_composable(scan, ss_globals_floor=FLOOR,
                             allow_unproven_ss=True)
    assert isinstance(depth, dict)


def test_the_opt_in_does_not_launder_an_ambiguous_constant():
    """The opt-in covers UNPROVEN COMPUTED addresses only.

    An above-floor CONSTANT takes a different path entirely: `ss_globals_only`
    reports `ss-access-crosses-globals-floor`, this gate never runs, and `ss`
    simply does not become a semantic data segment.  So the guarantee to test
    is that the opt-in cannot reach such a function at all -- asserting a
    refusal here would assert a behaviour that never existed.
    """
    from dos_re.lift.contracts import ss_globals_only
    scan = _scan(b"\x50\x36\xA1\x00\x30\x58\xC3")   # push; ss:[0x3000]; pop; ret
    ok, why = ss_globals_only(scan, FLOOR)
    assert not ok and why.startswith("ss-access-crosses-globals-floor"), why

    # and the address-space layer agrees, with the flag set:
    acc = classify_ss(scan, stack_floor=FLOOR)
    assert Space.AMBIGUOUS in {a.space for a in acc}
    verdict, reason = ss_data_verdict(acc, sp_independence_proven=True)
    assert not verdict and reason.startswith("ambiguous-ss-address")


def test_requires_proof_query_matches_the_gate():
    from dos_re.lift.addrspace import requires_sp_independence_proof
    assert requires_sp_independence_proof(_scan(b"\x36\x8A\x07"), FLOOR)
    assert not requires_sp_independence_proof(_scan(b"\x36\xA1\x06\x00"),
                                              FLOOR)
    assert not requires_sp_independence_proof(_scan(b"\x50\x58"), FLOOR)
