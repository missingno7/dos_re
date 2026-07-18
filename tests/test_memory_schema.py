"""The Memory Schema IR: impossible states must be UNCONSTRUCTIBLE.

Most of these assert a REFUSAL.  That is the point of the type: this same
tree produced two false greens from a verdict model whose contradictory
states were merely "not supposed to happen", so M4's ownership model enforces
its rules at construction instead of documenting them.

Every refusal here corresponds to a rule in docs/memory_schema.md §10.
"""
from __future__ import annotations

import pytest

from dos_re.lift.memory_schema import (Comparison, Evidence, Field_,
                                       Ownership, Region, Schema)


def _region(**kw):
    base = dict(name="r", segment="ds", base=0x1000, extent=16,
                ownership=Ownership.NATIVE, comparison=Comparison.EXACT)
    base.update(kw)
    return Region(**base)


# --- the comparison axis ----------------------------------------------------

def test_normalized_without_a_named_rule_refuses():
    """An unnamed normalizer is an unstated exception."""
    with pytest.raises(ValueError, match="named rule"):
        _region(comparison=Comparison.NORMALIZED)


def test_not_observed_without_a_reason_refuses():
    """'We do not look at it' is not evidence that nothing does."""
    with pytest.raises(ValueError, match="needs a reason"):
        _region(comparison=Comparison.NOT_OBSERVED)


def test_normalized_with_a_rule_is_accepted():
    r = _region(comparison=Comparison.NORMALIZED, rule="wrap16")
    assert r.rule == "wrap16"


# --- the ownership axis -----------------------------------------------------

def test_alias_view_must_name_one_canonical_owner():
    with pytest.raises(ValueError, match="canonical storage owner"):
        _region(ownership=Ownership.ALIAS_VIEW)


def test_alias_view_may_not_own_fields():
    """It never exports independently, so declaring fields on it would create
    a second owner for one byte range."""
    with pytest.raises(ValueError, match="never owns fields"):
        _region(ownership=Ownership.ALIAS_VIEW, canonical_owner="other",
                fields=(Field_(name="f", offset=0, width=2),))


def test_a_non_alias_region_may_not_claim_a_canonical_owner():
    with pytest.raises(ValueError, match="only an ALIAS_VIEW"):
        _region(canonical_owner="other")


def test_eliminated_requires_evidence_not_merely_exclusion():
    """§10: 'exclusion from a digest is not that evidence'.  This is the rule
    that stops ELIMINATED becoming a way to make divergence disappear."""
    with pytest.raises(ValueError, match="unobservable carrier state"):
        _region(ownership=Ownership.ELIMINATED,
                comparison=Comparison.NOT_OBSERVED, reason="carrier")


def test_eliminated_with_evidence_is_accepted():
    r = _region(ownership=Ownership.ELIMINATED,
                comparison=Comparison.NOT_OBSERVED, reason="carrier",
                evidence=Evidence(sites=("1010:0100",),
                                  note="written then overwritten before any "
                                       "boundary; no reader"))
    assert r.ownership is Ownership.ELIMINATED


def test_eliminated_bytes_cannot_also_be_compared_exactly():
    """They do not exist at runtime; comparing them is incoherent."""
    with pytest.raises(ValueError, match="cannot be compared"):
        _region(ownership=Ownership.ELIMINATED, comparison=Comparison.EXACT,
                evidence=Evidence(sites=("1010:0100",), note="carrier"))


def test_opaque_bytes_must_stay_compared():
    """HISTORICAL_OPAQUE preserves bytes precisely so the preservation can be
    checked; declaring them unobserved too removes the only check."""
    with pytest.raises(ValueError, match="removes the only check"):
        _region(ownership=Ownership.HISTORICAL_OPAQUE,
                comparison=Comparison.NOT_OBSERVED, reason="opaque")


# --- fields -----------------------------------------------------------------

def test_a_field_may_not_exceed_the_region():
    with pytest.raises(ValueError, match="exceeds extent"):
        _region(fields=(Field_(name="f", offset=14, width=4),))


def test_overlapping_fields_refuse():
    """An overlapping view is an ALIAS_VIEW, not a second owner."""
    with pytest.raises(ValueError, match="overlap at byte"):
        _region(fields=(Field_(name="a", offset=0, width=2),
                        Field_(name="b", offset=1, width=2)))


def test_adjacent_fields_are_fine():
    r = _region(fields=(Field_(name="a", offset=0, width=2),
                        Field_(name="b", offset=2, width=2)))
    assert len(r.fields) == 2


def test_a_field_read_both_signed_and_unsigned_needs_a_normalizer():
    """Storage facts and use-site interpretation are separate: mixed uses
    must not collapse into one native meaning by accident."""
    with pytest.raises(ValueError, match="BOTH signed"):
        Field_(name="f", offset=0, width=2, signed_uses=3, unsigned_uses=5,
               native_signed=True)


def test_mixed_uses_are_allowed_while_the_meaning_stays_unproven():
    """native_signed=None is the honest state: observed, not yet decided."""
    f = Field_(name="f", offset=0, width=2, signed_uses=3, unsigned_uses=5)
    assert f.native_signed is None


# --- schema-level -----------------------------------------------------------

def test_duplicate_region_names_refuse():
    with pytest.raises(ValueError, match="duplicate region names"):
        Schema(regions=(_region(name="x"), _region(name="x", base=0x2000)))


def test_an_alias_owner_must_exist_in_the_schema():
    with pytest.raises(ValueError, match="not in the schema"):
        Schema(regions=(_region(name="v", ownership=Ownership.ALIAS_VIEW,
                                canonical_owner="ghost"),))


def test_an_alias_may_not_point_at_another_alias():
    """Exactly ONE canonical storage owner; a chain of views has none."""
    with pytest.raises(ValueError, match="itself an ALIAS_VIEW"):
        Schema(regions=(
            _region(name="a", ownership=Ownership.ALIAS_VIEW,
                    canonical_owner="b"),
            _region(name="b", ownership=Ownership.ALIAS_VIEW,
                    canonical_owner="a")))


def test_a_valid_alias_pair_is_accepted():
    """The real shape: one offset reached through two segments, one owner."""
    s = Schema(regions=(
        _region(name="scroll", segment="ds"),
        _region(name="scroll_via_ss", segment="ss",
                ownership=Ownership.ALIAS_VIEW, canonical_owner="scroll")))
    assert len(s.regions) == 2


def test_the_digest_is_deterministic_and_content_sensitive():
    """Freshness (§11.6): every generated artifact embeds this, so a stale
    generation must be detectable -- the same mechanism that caught four
    stale runs during M3b."""
    a = Schema(regions=(_region(name="x"),), input_digest="abc")
    b = Schema(regions=(_region(name="x"),), input_digest="abc")
    assert a.digest() == b.digest()
    c = Schema(regions=(_region(name="x", extent=32),), input_digest="abc")
    assert c.digest() != a.digest()


def test_region_order_does_not_change_the_digest():
    """Declaration order is not content."""
    r1, r2 = _region(name="a"), _region(name="b", base=0x2000)
    assert Schema(regions=(r1, r2)).digest() == \
        Schema(regions=(r2, r1)).digest()
