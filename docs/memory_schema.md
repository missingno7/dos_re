# Memory schemas and state ownership

A memory schema is an optional per-region mechanism for representing original
memory layouts, declaring ownership, and projecting state across a verification
boundary. A game does not need a memory schema to use replay, lifting, authored
overrides, the Execution Atlas, or release planning.

## What the schema declares

`dos_re.lift.memory_schema` records:

- the stable identity and historical byte range of a region;
- fields and their evidenced widths, signedness, and address expressions;
- whether bytes are natively owned, retained as historical opaque data, alias
  another owner, or are proven eliminated;
- how the region is compared: exact, normalized by a named rule, or explicitly
  unobserved for a cited reason;
- the evidence and toolchain identity supporting those declarations.

Contradictory ownership and comparison policies fail at construction.
Game-specific ranges and field meanings live in the port.

## Generated bridge

`dos_re.lift.emit_codec` can generate import/export code between historical
bytes and detached values. The bridge has one writable authority at a time:

- a DOS-memory-backed implementation may keep historical bytes authoritative;
- a detached implementation may own ordinary values and generate historical
  bytes only for comparison;
- opaque data remains legal only under the selected implementation and release
  policy.

This is a region choice, not a project-wide milestone. Different regions may
use byte-backed views, detached values, generated codecs, or no schema at all.

## Evidence and verification

An ownership claim is meaningful only with evidence that all relevant reads,
writes, aliases, device access, and boundary effects are accounted for.
`dos_re.lift.ea_census` provides optional address-expression evidence. Replay
boundaries and canonical projections can verify a selected region
implementation without requiring the rest of the program to share its storage
representation.

Historical design rationale and the former staged roadmap are in
[the dos_re 2.0 memory-schema design](history/memory_schema_2.0.md).
