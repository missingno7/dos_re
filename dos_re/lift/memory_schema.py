"""The Memory Schema IR -- M4's ownership and identity boundary.

docs/memory_schema.md §9, §10, §14.3.  This is NOT a struct declaration
language: it is the machine-readable statement of WHO OWNS which historical
bytes, what evidence supports that, and how the declared boundary is judged.
A list of fields is necessary and nowhere near sufficient.

Two ORTHOGONAL axes (§10), because the original single policy enum mixed
different questions:

    ownership   NATIVE | HISTORICAL_OPAQUE | ALIAS_VIEW | ELIMINATED
                who owns or materializes these bytes?
    comparison  EXACT | NORMALIZED(rule) | NOT_OBSERVED(reason)
                how is the declared boundary judged?

Impossible combinations are REFUSED AT CONSTRUCTION rather than documented as
discouraged.  That distinction was learned the hard way in this same tree: a
verdict type whose contradictory states were merely "not supposed to happen"
produced two false greens before the invariants were enforced.

Game-agnostic: no address, offset, region or field of any particular program
appears here.  A port declares its own schema and this module validates it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum


class Ownership(Enum):
    """Who owns the bytes."""

    #: the native object is authoritative; historical bytes are generated
    #: from it for verification only
    NATIVE = "native"
    #: bytes preserved verbatim during migration.  Legal only while promoted
    #: native logic is PROVEN unable to depend on them -- preserving bytes for
    #: comparison does not make an unmodelled dependency memoryless, and
    #: before DETACHED every opaque range must become native-owned, proven
    #: eliminated, or proven outside the runtime state.
    HISTORICAL_OPAQUE = "historical_opaque"
    #: a second spelling of bytes owned elsewhere (e.g. the same offset
    #: reached through two segment registers).  Never exports independently.
    ALIAS_VIEW = "alias_view"
    #: proven unobservable carrier state, removed entirely
    ELIMINATED = "eliminated"


class Comparison(Enum):
    """How the declared boundary is judged."""

    EXACT = "exact"
    NORMALIZED = "normalized"        # requires a named rule
    NOT_OBSERVED = "not_observed"    # requires a stated reason


@dataclass(frozen=True)
class Evidence:
    """Why a claim in this schema is believed.

    ``sites`` are the census address-expression sites; ``closure`` the
    functions that must be owned together.  A claim with no evidence is not a
    claim, it is a hope -- so the validators below require it where the
    ownership decision depends on it.
    """

    sites: tuple = ()
    closure: tuple = ()
    note: str = ""


@dataclass(frozen=True)
class Field_:
    """One field.

    STORAGE facts and USE-SITE interpretation are deliberately separate
    (§9): the stored bits may be read as signed in one place and unsigned in
    another, so `SignedInt(bits=16)` must not be inferred from a single
    signed comparison.  ``signed_uses``/``unsigned_uses`` record what was
    OBSERVED; ``native_signed`` is None until one meaning is proven.
    """

    name: str
    offset: int
    width: int                       # storage width in bytes
    little_endian: bool = True
    signed_uses: int = 0             # count of use sites reading it signed
    unsigned_uses: int = 0
    native_signed: bool | None = None
    #: native arithmetic normalization, e.g. "wrap16" -- an ordinary Python
    #: int += delta must not silently acquire different overflow semantics
    normalize: str = ""
    evidence: Evidence = field(default_factory=Evidence)

    def __post_init__(self):
        if self.width <= 0:
            raise ValueError(f"field {self.name!r}: width must be positive")
        if self.offset < 0:
            raise ValueError(f"field {self.name!r}: negative offset")
        if self.native_signed is not None and self.signed_uses and \
                self.unsigned_uses and not self.normalize:
            raise ValueError(
                f"field {self.name!r} is read BOTH signed ({self.signed_uses} "
                f"sites) and unsigned ({self.unsigned_uses}); a single native "
                f"meaning needs a normalizer or must stay unproven")


@dataclass(frozen=True)
class Region:
    """One promotion unit -- an ownership closure, not an address interval.

    §9: `region + all access sites + aliases + pointer sources/escapes +
    relevant functions + boundary effects + lifetime rules`.  A small byte
    range with a large closure is not a small slice.
    """

    name: str
    segment: str
    base: int
    extent: int                      # bytes
    ownership: Ownership
    comparison: Comparison
    fields: tuple = ()
    #: NORMALIZED requires this; NOT_OBSERVED requires `reason`
    rule: str = ""
    reason: str = ""
    #: ALIAS_VIEW must name its single canonical owner
    canonical_owner: str = ""
    #: the boundary at which this region is observable
    observed_at: str = ""
    evidence: Evidence = field(default_factory=Evidence)

    def __post_init__(self):
        if self.extent <= 0:
            raise ValueError(f"region {self.name!r}: extent must be positive")

        # --- comparison axis ------------------------------------------------
        if self.comparison is Comparison.NORMALIZED and not self.rule:
            raise ValueError(
                f"region {self.name!r}: NORMALIZED comparison needs a named "
                f"rule; an unnamed normalizer is an unstated exception")
        if self.comparison is Comparison.NOT_OBSERVED and not self.reason:
            raise ValueError(
                f"region {self.name!r}: NOT_OBSERVED needs a reason -- "
                f"'we do not look at it' is not evidence that nothing does")

        # --- ownership axis -------------------------------------------------
        if self.ownership is Ownership.ALIAS_VIEW:
            if not self.canonical_owner:
                raise ValueError(
                    f"region {self.name!r}: ALIAS_VIEW must name its single "
                    f"canonical storage owner")
            if self.fields:
                raise ValueError(
                    f"region {self.name!r}: an ALIAS_VIEW never owns fields "
                    f"or exports independently; declare them on "
                    f"{self.canonical_owner!r}")
        elif self.canonical_owner:
            raise ValueError(
                f"region {self.name!r}: only an ALIAS_VIEW has a canonical "
                f"owner ({self.ownership.value} owns its own bytes)")

        if self.ownership is Ownership.ELIMINATED:
            # §10: "exclusion from a digest is not that evidence"
            if not (self.evidence.note and self.evidence.sites):
                raise ValueError(
                    f"region {self.name!r}: ELIMINATED requires evidence the "
                    f"bytes are unobservable carrier state -- the sites that "
                    f"touch them and why that is unobservable.  Excluding "
                    f"them from a comparison is NOT that evidence")
            if self.comparison is not Comparison.NOT_OBSERVED:
                raise ValueError(
                    f"region {self.name!r}: ELIMINATED bytes do not exist at "
                    f"runtime, so they cannot be compared "
                    f"{self.comparison.value}")

        if self.ownership is Ownership.HISTORICAL_OPAQUE and \
                self.comparison is Comparison.NOT_OBSERVED:
            raise ValueError(
                f"region {self.name!r}: HISTORICAL_OPAQUE preserves bytes so "
                f"they CAN be compared; declaring them unobserved as well "
                f"removes the only check that the preservation works")

        # --- fields fit, and do not overlap ---------------------------------
        seen = {}
        for f in self.fields:
            if f.offset + f.width > self.extent:
                raise ValueError(
                    f"region {self.name!r}: field {f.name!r} at {f.offset} "
                    f"+{f.width} exceeds extent {self.extent}")
            for off in range(f.offset, f.offset + f.width):
                if off in seen:
                    raise ValueError(
                        f"region {self.name!r}: fields {seen[off]!r} and "
                        f"{f.name!r} overlap at byte {off}; an overlapping "
                        f"view must be declared ALIAS_VIEW, not a second "
                        f"owner")
                seen[off] = f.name


@dataclass(frozen=True)
class Schema:
    """The whole declaration, with the freshness digest (§11.6)."""

    regions: tuple = ()
    #: digest of the INPUTS this schema was derived from (census, IR).  Every
    #: generated type, bridge, mask, rewrite and diagnostic embeds it; mixed
    #: or stale generations must refuse.  Same mechanism as the toolchain
    #: signature that caught four stale runs during M3b.
    input_digest: str = ""

    def __post_init__(self):
        names = [r.name for r in self.regions]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate region names: {sorted(dupes)}")
        by_name = {r.name: r for r in self.regions}
        for r in self.regions:
            if r.ownership is Ownership.ALIAS_VIEW:
                owner = by_name.get(r.canonical_owner)
                if owner is None:
                    raise ValueError(
                        f"region {r.name!r}: canonical owner "
                        f"{r.canonical_owner!r} is not in the schema")
                if owner.ownership is Ownership.ALIAS_VIEW:
                    raise ValueError(
                        f"region {r.name!r}: canonical owner "
                        f"{r.canonical_owner!r} is itself an ALIAS_VIEW; "
                        f"exactly one canonical storage owner is required")

    def digest(self) -> str:
        """Content digest of the schema itself, for generated-artifact
        freshness.  Deterministic: sorted keys, no float, no set order."""
        payload = json.dumps(self.as_json(), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def as_json(self) -> dict:
        return {
            "input_digest": self.input_digest,
            "regions": [{
                "name": r.name, "segment": r.segment, "base": r.base,
                "extent": r.extent, "ownership": r.ownership.value,
                "comparison": r.comparison.value, "rule": r.rule,
                "reason": r.reason, "canonical_owner": r.canonical_owner,
                "observed_at": r.observed_at,
                "evidence": {"sites": list(r.evidence.sites),
                             "closure": list(r.evidence.closure),
                             "note": r.evidence.note},
                "fields": [{
                    "name": f.name, "offset": f.offset, "width": f.width,
                    "little_endian": f.little_endian,
                    "signed_uses": f.signed_uses,
                    "unsigned_uses": f.unsigned_uses,
                    "native_signed": f.native_signed,
                    "normalize": f.normalize,
                } for f in r.fields],
            } for r in sorted(self.regions, key=lambda r: r.name)],
        }
