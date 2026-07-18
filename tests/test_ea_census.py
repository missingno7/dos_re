"""The EA census: symbolic address expressions from pinned IR bytes.

Written against KNOWN encodings rather than against whatever the corpus
happens to produce -- a census validated by its own output would agree with
itself no matter what it decoded.

The alias test is the one that already changed a plan: the M4 design record
prescribed a first region on the belief that it had "no aliasing question",
and the census is what disproves that class of claim.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.ea_census import AddressExpr, Blocker, Census, sites_of


def _scan(code: bytes) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90   # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        ip = i.next_ip
    return s


def _one(code: bytes):
    got = [x for x in sites_of(_scan(code), "1010:0000")]
    assert len(got) == 1, got
    return got[0]


def test_direct_disp16_is_a_static_ds_site():
    """`mov ax, [0x1234]` -- mod=0 rm=6, the fixed-base global form."""
    site = _one(bytes.fromhex("a1 3412".replace(" ", "")))
    assert isinstance(site, AddressExpr)
    assert site.is_static and site.segment == "ds"
    assert site.disp == 0x1234 and site.width == 2


def test_a_bp_effective_address_defaults_to_ss():
    """[bp+disp] defaults to SS -- the frame-access rule.  Getting this
    backwards would file every frame slot as a ds global."""
    site = _one(bytes.fromhex("8b4604"))       # mov ax, [bp+4]
    assert site.segment == "ss" and site.base == "bp"
    assert site.disp == 4 and not site.is_static


def test_a_segment_override_wins_over_the_default():
    site = _one(bytes.fromhex("368b4604"))     # mov ax, ss:[bp+4]
    assert site.segment == "ss"
    site2 = _one(bytes.fromhex("3e8b07"))      # mov ax, ds:[bx]
    assert site2.segment == "ds" and site2.base == "bx"


def test_an_indexed_site_records_base_and_index():
    """[bx+si+disp] is the array shape: base + index is the stride evidence."""
    site = _one(bytes.fromhex("8b4008"))       # mov ax, [bx+si+8]
    assert site.base == "bx" and site.index == "si"
    assert site.disp == 8 and not site.is_static


def test_width_comes_from_the_opcode_not_its_parity():
    """0x8C (mov r/m16, Sreg) is even-opcoded yet 2 bytes; 0xC4 (LES) is even
    yet FOUR.  Parity is not a width rule."""
    assert _one(bytes.fromhex("8c06 3412".replace(" ", ""))).width == 2
    assert _one(bytes.fromhex("c406 3412".replace(" ", ""))).width == 4
    assert _one(bytes.fromhex("8a06 3412".replace(" ", ""))).width == 1


def test_an_unknown_opcode_is_a_reported_blocker_not_a_guess():
    """An approximated address silently widens or narrows an ownership
    closure, and the closure is what promotion rests on."""
    # x87 (0xD8-0xDF) reads 4/8/10 bytes depending on the form -- genuinely
    # unmodelled here, so it must REPORT rather than be sized by guess.
    got = sites_of(_scan(bytes.fromhex("d806 3412".replace(" ", ""))),
                   "1010:0000")
    assert any(isinstance(x, Blocker) and "width-unknown" in x.reason
               for x in got), got


def test_pop_into_memory_is_a_two_byte_write():
    """`pop [0x1234]` was missing from the width table -- found by the test
    above needing an unmodelled opcode and discovering this one was real."""
    site = _one(bytes.fromhex("8f06 3412".replace(" ", "")))
    assert site.width == 2 and site.writes and site.is_static


def test_register_operands_are_not_memory_sites():
    assert sites_of(_scan(bytes.fromhex("89c3")), "1010:0000") == []


# --- clustering and the alias report ---------------------------------------

def _expr(seg, disp, key="1010:0000", base=None, index=None):
    return AddressExpr(key, 0, seg, base, index, disp, 2, False)


def test_static_sites_cluster_by_segment_and_offset():
    c = Census(sites=[_expr("ds", 0x10), _expr("ds", 0x10, key="1010:0100"),
                      _expr("ds", 0x20)])
    cl = c.static_clusters()
    assert len(cl[("ds", 0x10)]) == 2 and len(cl[("ds", 0x20)]) == 1


def test_indexed_sites_cluster_by_base_and_index():
    c = Census(sites=[_expr("ds", 0, base="bx", index="si"),
                      _expr("ds", 5, base="bx", index="si"),
                      _expr("ds", 0, base="bp", index=None)])
    cl = c.indexed_clusters()
    assert sorted(s.disp for s in cl[("ds", "bx", "si")]) == [0, 5]


def test_the_same_offset_through_two_segments_is_reported_as_an_alias():
    """THE check that changed the M4 plan.  A region reached as both ds:X and
    ss:X is not the small slice its byte extent suggests."""
    c = Census(sites=[_expr("ds", 0x00), _expr("ss", 0x00),
                      _expr("ss", 0x00, key="1010:0100"),
                      _expr("ds", 0x40)])
    al = c.segment_aliases()
    assert 0x00 in al and al[0x00] == {"ds": 1, "ss": 2}
    assert 0x40 not in al, "a single-segment offset is not an alias"


def test_closure_spans_every_segment_spelling():
    """The ownership closure must count functions using EITHER spelling --
    counting one segment made a ~56-function closure look like a handful."""
    c = Census(sites=[_expr("ds", 0x00, key="A"), _expr("ss", 0x00, key="B"),
                      _expr("ss", 0x00, key="C")])
    assert c.closure(0x00) == {"A", "B", "C"}


@pytest.mark.parametrize("rm,base,index", [
    (0, "bx", "si"), (1, "bx", "di"), (2, "bp", "si"), (3, "bp", "di"),
    (4, "si", None), (5, "di", None), (7, "bx", None),
])
def test_every_modrm16_form_decodes(rm, base, index):
    """mod=1 (disp8) so rm=6 is [bp+disp] rather than the disp16 form."""
    code = bytes([0x8B, 0x40 | rm, 0x04])
    site = _one(code)
    assert (site.base, site.index) == (base, index)
