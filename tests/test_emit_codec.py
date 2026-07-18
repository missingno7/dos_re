"""The codec generator: the law must HOLD on what it emits, and the shapes it
cannot honour must refuse rather than emit something plausible.

The law test itself is generated, but this suite does not trust generation:
it executes the generated module and the generated test body directly, so a
generator bug fails HERE, not in some future port's CI.
"""
from __future__ import annotations

import types

import pytest

from dos_re.lift.emit_codec import CodecRefusal, emit_codec, emit_codec_law_test
from dos_re.lift.memory_schema import (Comparison, Evidence, Field_,
                                       Ownership, Region, Schema)


def _region(**kw):
    base = dict(
        name="pos", segment="ds", base=0x0100, extent=8,
        ownership=Ownership.NATIVE, comparison=Comparison.EXACT,
        fields=(Field_(name="x", offset=0, width=2),
                Field_(name="y", offset=2, width=2),
                Field_(name="flags", offset=5, width=1)))
    base.update(kw)
    return Region(**base)


def _load(src: str, name: str = "gen_codec"):
    mod = types.ModuleType(name)
    exec(compile(src, name + ".py", "exec"), mod.__dict__)
    return mod


class _Mem:
    def __init__(self):
        self.b = {}

    def rb(self, seg, off):
        return self.b.setdefault(((seg << 4) + off) & 0xFFFFF,
                                 (off * 37 + 11) & 0xFF)

    def rw(self, seg, off):
        return self.rb(seg, off) | (self.rb(seg, off + 1) << 8)

    def wb(self, seg, off, v):
        self.b[((seg << 4) + off) & 0xFFFFF] = v & 0xFF

    def ww(self, seg, off, v):
        self.wb(seg, off, v)
        self.wb(seg, off + 1, v >> 8)


def test_the_codec_law_holds_on_generated_output():
    """Import then export: every byte of the extent identical, including the
    padding bytes no field covers (offsets 4, 6, 7 here)."""
    r = _region()
    schema = Schema(regions=(r,), input_digest="test")
    mod = _load(emit_codec(r, schema, seg_expr="seg"))
    mem = _Mem()
    seg = 0x2000
    before = [mem.rb(seg, mod.BASE + o) for o in range(mod.EXTENT)]
    vals = mod.import_region(mem, seg=seg)
    mod.export_region(mem, vals, seg=seg)
    after = [mem.rb(seg, mod.BASE + o) for o in range(mod.EXTENT)]
    assert after == before


def test_imported_values_are_plain_and_detached():
    """No mem handle survives into the values: mutate the image afterwards
    and the values must not move.  A lazy view fails this -- the field_06
    anti-pattern gate."""
    r = _region()
    schema = Schema(regions=(r,), input_digest="test")
    mod = _load(emit_codec(r, schema, seg_expr="seg"))
    mem = _Mem()
    vals = mod.import_region(mem, seg=0x2000)
    snap = dict(vals)
    for o in range(mod.EXTENT):
        mem.wb(0x2000, mod.BASE + o, 0xEE)
    assert vals == snap


def test_a_signed_field_round_trips_negative_values():
    r = _region(fields=(Field_(name="dx", offset=0, width=2,
                               signed_uses=4, native_signed=True),))
    schema = Schema(regions=(r,), input_digest="test")
    mod = _load(emit_codec(r, schema, seg_expr="seg"))
    mem = _Mem()
    mem.ww(0x2000, mod.BASE, 0x8000)          # -32768 as stored bits
    vals = mod.import_region(mem, seg=0x2000)
    assert vals["dx"] == -32768, "signed import must produce the native value"
    mod.export_region(mem, vals, seg=0x2000)
    assert mem.rw(0x2000, mod.BASE) == 0x8000, "and export the same bits"


def test_stale_generation_refuses_before_transcoding():
    """§11.6 at runtime: a caller pinned to a different schema digest must be
    refused BEFORE any bytes move."""
    r = _region()
    schema = Schema(regions=(r,), input_digest="test")
    mod = _load(emit_codec(r, schema, seg_expr="seg"))
    mod.check_generation(mod.SCHEMA_DIGEST)   # same generation: fine
    with pytest.raises(RuntimeError, match="STALE GENERATION"):
        mod.check_generation("0" * 16)


def test_alias_views_refuse_a_codec():
    """§10: an alias never exports independently."""
    owner = _region(name="owner")
    alias = Region(name="alias", segment="ss", base=0x0100, extent=8,
                   ownership=Ownership.ALIAS_VIEW, comparison=Comparison.EXACT,
                   canonical_owner="owner")
    schema = Schema(regions=(owner, alias), input_digest="test")
    with pytest.raises(CodecRefusal, match="never exports independently"):
        emit_codec(alias, schema, seg_expr="seg")


def test_eliminated_refuses_a_codec():
    r = Region(name="gone", segment="ds", base=0, extent=2,
               ownership=Ownership.ELIMINATED,
               comparison=Comparison.NOT_OBSERVED, reason="carrier",
               evidence=Evidence(sites=("1010:0100",), note="overwritten "
                                 "before any boundary; no reader"))
    schema = Schema(regions=(r,), input_digest="test")
    with pytest.raises(CodecRefusal, match="nothing to transcode"):
        emit_codec(r, schema, seg_expr="seg")


def test_native_with_no_fields_refuses():
    """Detaching an opaque blob as NATIVE would claim ownership of bytes the
    schema has not modelled."""
    r = _region(fields=())
    schema = Schema(regions=(r,), input_digest="test")
    with pytest.raises(CodecRefusal, match="opaque blob"):
        emit_codec(r, schema, seg_expr="seg")


def test_the_generated_law_test_passes_and_can_fail():
    """Run the GENERATED test body, then mutation-check it: a codec whose
    export skips padding must make the generated law test FAIL.  A law test
    that cannot fail would be the vacuous-check pattern again."""
    r = _region()
    schema = Schema(regions=(r,), input_digest="test")
    codec_src = emit_codec(r, schema, seg_expr="seg")
    import sys
    mod = _load(codec_src, "gen_codec_law")
    sys.modules["gen_codec_law"] = mod
    try:
        test_src = emit_codec_law_test(r, schema, seg_expr="seg",
                                       module="gen_codec_law")
        tmod = _load(test_src, "gen_codec_law_test")
        tmod.test_codec_law_import_then_export_is_byte_identical()
        tmod.test_imported_values_are_detached()

        # mutation: break padding preservation in the export -- write zeros
        # instead of the imported bytes, keeping the syntax valid
        broken_src = codec_src.replace(
            'mem.wb(seg, BASE + o, b)', 'mem.wb(seg, BASE + o, 0)  # MUTATED')
        assert broken_src != codec_src, "mutation did not apply"
        sys.modules["gen_codec_law"] = _load(broken_src, "gen_codec_law")
        tmod2 = _load(test_src, "gen_codec_law_test2")
        with pytest.raises(AssertionError):
            tmod2.test_codec_law_import_then_export_is_byte_identical()
    finally:
        sys.modules.pop("gen_codec_law", None)
