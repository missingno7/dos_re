"""The effects layer must agree with every table it replaces.

No consumer is switched over until this passes.  A disagreement here is
EVIDENCE about one of the two descriptions, never permission to relax the
comparison -- the whole point of the exercise is that two sources of truth
had already drifted, so a test that shrugs at a mismatch would reintroduce
exactly the defect being removed.

Where the effects layer deliberately differs from an incumbent table, the case is
named individually with the reason, and the difference is asserted in BOTH
directions: what the old table said, and what the new layer says.  A silent
allow-list would be indistinguishable from a bug.
"""
from __future__ import annotations

import pytest

from dos_re.lift import cfg as cfg_mod
from dos_re.lift import contracts as contracts_mod
from dos_re.lift import ea_census as census_mod
from dos_re.lift import effects as fx
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import _flags_defined_by


def _decode(code: bytes):
    fetch = lambda o: code[o] if o < len(code) else 0x90    # noqa: E731
    return decode_one(fetch, 0)


def _scan(code: bytes) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90    # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        ip = i.next_ip
    return s


# A modrm byte with mod=00 rm=110 -> [disp16], so every opcode below gets a
# real memory operand with a static address, which is the case all the incumbent
# tables were written to describe.
def _mem_form(op: int, reg: int = 0, imm: bytes = b"") -> bytes:
    modrm = bytes([(reg << 3) | 0x06])
    return bytes([op]) + modrm + b"\x49\xA9" + imm


# --------------------------------------------------------------------------
# 1. ACCESS_WIDTH  (ea_census)

#: opcodes where the effects layer INTENTIONALLY disagrees with ACCESS_WIDTH.
#: Each is a defect in the table, verified against the ISA, not a tolerance.
_WIDTH_DIFFERENCES = {
    # LEA computes an effective address; it performs NO memory access.  The
    # census listed it at width 2, so every `lea bx,[X]` was recorded as a
    # READ of X -- inventing a toucher for any region so addressed.
    0x8D: "lea touches no memory",
}


@pytest.mark.parametrize("op", sorted(census_mod.ACCESS_WIDTH))
def test_width_matches_the_census_table(op):
    imm = b"\x00\x00" if op in (0x81, 0xC7) else (
        b"\x00" if op in (0x80, 0x83, 0xC6) else b"")
    inst = _decode(_mem_form(op, imm=imm))
    eff = fx.effects_of(inst)
    assert eff.refusal is None, f"{op:#04x} refused: {eff.refusal}"
    if op in _WIDTH_DIFFERENCES:
        assert eff.mem_width is None, _WIDTH_DIFFERENCES[op]
        return
    assert eff.mem_width == census_mod.ACCESS_WIDTH[op], (
        f"{op:#04x}: effects says {eff.mem_width}, "
        f"ACCESS_WIDTH says {census_mod.ACCESS_WIDTH[op]}")


def test_lea_is_not_a_memory_access():
    """Named on its own so the difference above cannot be read as a rounding."""
    inst = _decode(_mem_form(0x8D))
    assert census_mod.ACCESS_WIDTH[0x8D] == 2, "the old table claimed a width"
    assert fx.effects_of(inst).mem == (), "lea reads no memory"


# --------------------------------------------------------------------------
# 2. _SS_ACCESS_WIDTH  (contracts)

@pytest.mark.parametrize("op", sorted(contracts_mod._SS_ACCESS_WIDTH))
def test_width_matches_the_ss_globals_table(op):
    imm = b"\x00\x00" if op in (0x81, 0xC7) else (
        b"\x00" if op in (0x80, 0x83, 0xC6) else b"")
    inst = _decode(b"\x36" + _mem_form(op, imm=imm))       # ss: override
    eff = fx.effects_of(inst)
    assert eff.refusal is None
    assert eff.mem_width == contracts_mod._SS_ACCESS_WIDTH[op]
    assert all(m.segment == "ss" for m in eff.mem), "override must be honoured"


def test_the_two_width_tables_never_contradicted_each_other():
    """Both are now derived, but record that they DID agree where they
    overlapped -- the drift was in coverage, not in values."""
    shared = set(census_mod.ACCESS_WIDTH) & set(contracts_mod._SS_ACCESS_WIDTH)
    assert shared, "the tables overlap"
    for op in shared:
        assert census_mod.ACCESS_WIDTH[op] == contracts_mod._SS_ACCESS_WIDTH[op]


# --------------------------------------------------------------------------
# 3. _STORE_OPS  (ea_census "this writes memory")

#: opcodes _STORE_OPS calls writers that do NOT write memory.  All are cases
#: where write-ness depends on the modrm REG field, which a per-opcode table
#: cannot see -- plus cmp, which simply never writes.
_STORE_FALSE_POSITIVES = {
    0x38: (0, "cmp rm8,r8 reads its destination"),
    0x39: (0, "cmp rm16,r16 reads its destination"),
    0x80: (7, "grp1 /7 is cmp"),
    0x81: (7, "grp1 /7 is cmp"),
    0x83: (7, "grp1 /7 is cmp"),
    0xF6: (0, "grp3 /0 is test"),
    0xF7: (0, "grp3 /0 is test"),
    0xFF: (6, "grp5 /6 is push -- it reads the operand"),
}


@pytest.mark.parametrize("op", sorted(census_mod._STORE_OPS))
def test_store_classification_matches_where_the_table_can_be_right(op):
    """For each opcode _STORE_OPS calls a writer, pick a sub-opcode that
    really is one and require agreement."""
    writer_reg = {0x80: 0, 0x81: 0, 0x83: 0, 0xF6: 2, 0xF7: 2,
                  0xFE: 0, 0xFF: 0, 0x8F: 0}.get(op, 0)
    imm = b"\x00\x00" if op in (0x81, 0xC7) else (
        b"\x00" if op in (0x80, 0x83, 0xC6, 0xC0, 0xC1) else b"")
    inst = _decode(_mem_form(op, reg=writer_reg, imm=imm))
    eff = fx.effects_of(inst)
    assert eff.refusal is None, f"{op:#04x} refused: {eff.refusal}"
    if op in (0x38, 0x39):
        assert not eff.writes_memory, _STORE_FALSE_POSITIVES[op][1]
        return
    assert eff.writes_memory, (
        f"{op:#04x}/{writer_reg} should write memory")


@pytest.mark.parametrize("op,reg,why", [
    (op, reg, why) for op, (reg, why) in _STORE_FALSE_POSITIVES.items()])
def test_store_ops_false_positives_are_real(op, reg, why):
    """The old table said 'writes'; the ISA says otherwise.  Assert both, so
    this documents a fixed defect rather than hiding a disagreement."""
    imm = b"\x00\x00" if op == 0x81 else (b"\x00" if op in (0x80, 0x83) else b"")
    inst = _decode(_mem_form(op, reg=reg, imm=imm))
    assert op in census_mod._STORE_OPS, "the old table called this a writer"
    eff = fx.effects_of(inst)
    assert eff.refusal is None
    assert eff.reads_memory, f"{why}: it does read"
    assert not eff.writes_memory, why


# --------------------------------------------------------------------------
# 4. _RM_WRITE_OPS  (cfg: UNCONDITIONAL r/m writers)
#
# This table answers a NARROWER question than _STORE_OPS -- its docstring says
# sub-op-dependent writers are resolved elsewhere -- so the correct assertion
# is containment, not equality.

@pytest.mark.parametrize("op", sorted(cfg_mod._RM_WRITE_OPS))
def test_unconditional_rm_writers_do_write(op):
    imm = b"\x00" if op in (0xC0, 0xC1) else b""
    inst = _decode(_mem_form(op, imm=imm))
    eff = fx.effects_of(inst)
    assert eff.refusal is None, f"{op:#04x} refused: {eff.refusal}"
    assert eff.writes_memory, f"{op:#04x} is listed as an unconditional writer"


def test_the_shift_opcodes_the_census_could_not_size():
    """0xC0/0xC1 are writers per cfg but absent from ACCESS_WIDTH, so the
    census REFUSED them (safe, but blind).  The effects layer sizes them."""
    assert 0xC0 in cfg_mod._RM_WRITE_OPS and 0xC1 in cfg_mod._RM_WRITE_OPS
    assert 0xC0 not in census_mod.ACCESS_WIDTH
    assert fx.effects_of(_decode(_mem_form(0xC0, imm=b"\x01"))).mem_width == 1
    assert fx.effects_of(_decode(_mem_form(0xC1, imm=b"\x01"))).mem_width == 2


# --------------------------------------------------------------------------
# 5. _flags_defined_by  (emit_cpuless) -- the subtle one

_FLAG_CASES = [
    (b"\x01\x06\x49\xA9", "add [x],ax"),
    (b"\x39\x06\x49\xA9", "cmp [x],ax"),
    (b"\x85\x06\x49\xA9", "test [x],ax"),
    (b"\x40", "inc ax"),
    (b"\x48", "dec ax"),
    (b"\xF8", "clc"), (b"\xF9", "stc"),
    (b"\xFA", "cli"), (b"\xFB", "sti"),
    (b"\xFC", "cld"), (b"\xFD", "std"),
    (b"\x9D", "popf"),
    (b"\xCD\x21", "int 21h"),
    (b"\xD1\xE0", "shl ax,1"),
    (b"\xD1\xC8", "ror ax,1"),
    (b"\xD3\xE0", "shl ax,cl"),
    (b"\xC1\xE0\x01", "shl ax,1 (imm form)"),
    (b"\xC1\xE0\x05", "shl ax,5"),
    (b"\xC1\xE0\x00", "shl ax,0 -- defines nothing"),
    (b"\xC1\xC8\x03", "ror ax,3"),
    (b"\xA6", "cmpsb"),
    (b"\xF3\xA6", "repe cmpsb -- count may be zero"),
    (b"\xAE", "scasb"),
    (b"\xF2\xAE", "repne scasb"),
    (b"\xF7\xE1", "mul cx"),
    (b"\xF7\xD9", "neg cx"),
    (b"\xFE\x06\x49\xA9", "inc byte [x]"),
    (b"\xFF\x36\x49\xA9", "push word [x]"),
    (b"\x88\x06\x49\xA9", "mov [x],al -- no flags"),
    (b"\x8B\x06\x49\xA9", "mov ax,[x] -- no flags"),
    (b"\x50", "push ax"), (b"\x58", "pop ax"),
]


@pytest.mark.parametrize("code,desc", _FLAG_CASES)
def test_flags_written_match_the_emitter(code, desc):
    inst = _decode(code)
    eff = fx.effects_of(inst)
    assert eff.refusal is None, f"{desc}: refused {eff.refusal}"
    assert eff.flags_written == _flags_defined_by(inst), desc


# --------------------------------------------------------------------------
# 6. string instructions -- what the census could not see at all

@pytest.mark.parametrize("code,mnem,segs", [
    (b"\xA4", "movsb", {"ds", "es"}),
    (b"\xA5", "movsw", {"ds", "es"}),
    (b"\xA6", "cmpsb", {"ds", "es"}),
    (b"\xAA", "stosb", {"es"}),
    (b"\xAB", "stosw", {"es"}),
    (b"\xAC", "lodsb", {"ds"}),
    (b"\xAD", "lodsw", {"ds"}),
    (b"\xAE", "scasb", {"es"}),
])
def test_string_ops_are_visible_with_their_segments(code, mnem, segs):
    eff = fx.effects_of(_decode(code))
    assert eff.refusal is None
    assert eff.mem, f"{mnem} must be a visible memory access"
    assert {m.segment for m in eff.mem} == segs
    assert all(m.implicit for m in eff.mem)


def test_string_source_honours_an_override_but_destination_does_not():
    """`ss: movsb` reads ss:si and still writes es:di -- es is not
    overridable on x86, and a layer that applied the prefix to both sides
    would misattribute every prefixed block move."""
    eff = fx.effects_of(_decode(b"\x36\xA4"))
    segs = {(m.segment, m.writes) for m in eff.mem}
    assert ("ss", False) in segs, "source redirected to ss"
    assert ("es", True) in segs, "destination stays es"


def test_lods_writes_no_memory_and_stos_reads_none():
    lods = fx.effects_of(_decode(b"\xAC"))
    assert lods.reads_memory and not lods.writes_memory
    stos = fx.effects_of(_decode(b"\xAA"))
    assert stos.writes_memory and not stos.reads_memory


def test_scas_and_cmps_read_their_destination_without_writing_it():
    for code in (b"\xAE", b"\xA6"):
        eff = fx.effects_of(_decode(code))
        assert eff.reads_memory
        assert not eff.writes_memory, "a comparison writes nothing"


def test_string_ops_step_their_pointers_and_consume_df():
    for code, ptrs in ((b"\xA4", ("si", "di")), (b"\xAA", ("di",)),
                       (b"\xAC", ("si",))):
        eff = fx.effects_of(_decode(code))
        assert eff.ptr_updates == ptrs
        assert "df" in eff.flags_read, "the step direction is DF-dependent"


def test_rep_prefix_brings_cx_into_the_effects():
    assert "cx" in fx.effects_of(_decode(b"\xF3\xAA")).implicit_reads
    assert "cx" not in fx.effects_of(_decode(b"\xAA")).implicit_reads


# --------------------------------------------------------------------------
# 7. refusal -- the property that makes the layer safe to trust

def test_an_unmodelled_opcode_refuses_rather_than_reporting_no_memory():
    """The failure mode this layer exists to prevent: silence that reads as
    'touches nothing'.  x87 escapes are the honest example."""
    eff = fx.effects_of(_decode(b"\xD8\x06\x49\xA9"))
    assert eff.refusal is not None
    assert "unmodelled" in eff.refusal
    assert eff.mem == ()


def test_an_unmodelled_group_subop_refuses():
    eff = fx.effects_of(_decode(_mem_form(0xFE, reg=7)))
    assert eff.refusal is not None and "subop" in eff.refusal


def test_register_forms_report_no_memory_access():
    """mod=3 is a register operand: no memory, and no refusal either."""
    eff = fx.effects_of(_decode(b"\x01\xC3"))          # add bx,ax
    assert eff.refusal is None and eff.mem == ()


# --------------------------------------------------------------------------
# 8. bp defaults to ss, and overrides win -- the segment rule in one place

def test_bp_based_addressing_defaults_to_ss():
    eff = fx.effects_of(_decode(b"\x8B\x46\x04"))      # mov ax,[bp+4]
    assert [m.segment for m in eff.mem] == ["ss"]


def test_bx_based_addressing_defaults_to_ds():
    eff = fx.effects_of(_decode(b"\x8B\x07"))          # mov ax,[bx]
    assert [m.segment for m in eff.mem] == ["ds"]


def test_an_override_beats_the_bp_default():
    eff = fx.effects_of(_decode(b"\x26\x8B\x46\x04"))  # mov ax,es:[bp+4]
    assert [m.segment for m in eff.mem] == ["es"]


def test_the_effects_segment_rule_matches_the_census_one():
    """Both compute it today; they must not diverge before the census is
    migrated onto the shared rule."""
    for code in (b"\x8B\x46\x04", b"\x8B\x07", b"\x26\x8B\x46\x04",
                 b"\x36\x8B\x07", b"\x8B\x06\x49\xA9"):
        inst = _decode(code)
        assert fx.segment_of(inst) == census_mod._segment_of(inst)


# --------------------------------------------------------------------------
# 9. corpus coverage -- the layer must describe the real program

def test_every_opcode_the_census_sizes_is_described():
    """Anything the old width table could size, the new layer must handle."""
    unrefused = []
    for op in sorted(census_mod.ACCESS_WIDTH):
        imm = b"\x00\x00" if op in (0x81, 0xC7) else (
            b"\x00" if op in (0x80, 0x83, 0xC6) else b"")
        eff = fx.effects_of(_decode(_mem_form(op, imm=imm)))
        if eff.refusal is not None:
            unrefused.append((hex(op), eff.refusal))
    assert not unrefused, f"regressed to refusal: {unrefused}"
