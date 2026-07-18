"""slice 9: `ss:[const]` globals inside a function that also uses the stack.

`ss_is_data_segment` refuses a function that does BOTH stack traffic and
ss-addressed data, because in the MECHANICAL form the two share one segment
and cannot be told apart.  A DE-STACKED core can tell them apart: its frame is
Python locals and it writes no stack memory, so constant offsets below the
globals floor are disjoint from the frame by construction.

What must stay refused matters more than what is promoted, so most of these
lock the refusals: a computed ss address could reach the live frame, and an
offset above the floor is not provably a global.
"""
from __future__ import annotations

import pytest

from dos_re.lift.contracts import (SS_GLOBALS_FLOOR, ss_globals_only)


class _I:
    """Minimal instruction stand-in matching the fields the predicate reads."""

    def __init__(self, op, *, prefixes=(), modrm=None, mod=None, rm=None,
                 disp=None, imm=None):
        self.op = op
        self.prefixes = tuple(prefixes)
        self.modrm = modrm
        self.mod = mod
        self.rm = rm
        self.disp = disp
        self.imm = imm


class _Scan:
    def __init__(self, insts):
        self.insts = {n: i for n, i in enumerate(insts)}


def _moffs(off):
    """`mov ax, ss:[off]` -- offset in the immediate, no ModRM."""
    return _I(0xA1, prefixes=(0x36,), imm=off)


def _disp16(off):
    """`mov ss:[off], ax` via ModRM mod=0 rm=6."""
    return _I(0x89, prefixes=(0x36,), modrm=0x06, mod=0, rm=6, disp=off)


def test_constant_globals_below_the_floor_are_promoted():
    ok, offs = ss_globals_only(_Scan([_moffs(0x00), _disp16(0x10)]))
    assert ok
    assert offs == frozenset({0x00, 0x10})


def test_the_real_lemmings_offsets_qualify():
    """Every ss:-override in the corpus targets 0x00-0x10 -- the render-scroll
    and logical-camera globals."""
    ok, offs = ss_globals_only(
        _Scan([_moffs(o) for o in (0x0, 0x2, 0x4, 0x6, 0x8, 0xA, 0xC, 0xE)]))
    assert ok and len(offs) == 8


def test_computed_ss_address_is_refused():
    """`ss:[bx]` could reach the live frame -- refusing is the whole point."""
    ok, why = ss_globals_only(
        _Scan([_moffs(0x02),
               _I(0x8B, prefixes=(0x36,), modrm=0x07, mod=0, rm=7)]))
    assert not ok
    assert why == "computed-ss-address"


def test_offset_above_the_floor_is_refused():
    ok, why = ss_globals_only(_Scan([_moffs(SS_GLOBALS_FLOOR)]))
    assert not ok
    assert why.startswith("ss-offset-above-globals-floor")


def test_offset_just_below_the_floor_is_accepted():
    """The boundary itself must be exercised, or the floor could be off by one
    and no test would notice."""
    ok, offs = ss_globals_only(_Scan([_moffs(SS_GLOBALS_FLOOR - 1)]))
    assert ok and offs == frozenset({SS_GLOBALS_FLOOR - 1})


def test_a_function_with_no_ss_override_does_not_qualify():
    """No ss: access at all means there is nothing to promote; the caller must
    not treat that as 'ss is semantic'."""
    ok, why = ss_globals_only(_Scan([_I(0x90)]))
    assert not ok
    assert why == "no-ss-globals"


def test_stack_offset_is_far_above_the_floor():
    """Sanity-check the premise the tier rests on: Lemmings boots sp=0x4AF4,
    so the live stack cannot collide with a sub-0x200 global."""
    assert 0x4AF4 > SS_GLOBALS_FLOOR * 2


@pytest.mark.parametrize("off", [0x1FF, 0x100, 0x0])
def test_promoted_offsets_are_all_below_the_floor(off):
    ok, offs = ss_globals_only(_Scan([_moffs(off)]))
    assert ok and all(o < SS_GLOBALS_FLOOR for o in offs)
