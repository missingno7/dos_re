# Generated implementations

Automatic lifting is one optional way to provide implementations for original
program identities. It is useful when literal generated Python gives a port
enough speed, reachability, or executable structure without requiring a manual
rewrite.

The lifter is not a global recovery mode. A project can combine:

- original interpreted functions;
- generated instruction-shaped functions;
- generated CPU-independent functions;
- ABI-recovered generated functions;
- authored faithful replacements, enhancements, and behavioral modifications.

These choices are made per function or region by the implementation catalog and
execution plan.

## Shared mechanism

The 16-bit and 32-bit lifters share the same basic contract:

1. obtain pinned code bytes and a decoded control-flow shape;
2. refuse unsupported or uncertain behavior explicitly;
3. emit a deterministic candidate with source and toolchain provenance;
4. register that candidate as an available generated implementation;
5. verify relevant executions against the original oracle.

Input may come from retained [Recovery IR](recovery_ir.md) or from a targeted
snapshot scan. Both paths use the same decoder, scanner, and emitter contracts.
A direct scan is useful for a focused experiment; retained IR is useful when
the discovered static structure should be reused and indexed.

Generated modules are disposable build products. Module and symbol names are
display metadata, not stable program identities. Never hand-edit a generated
corpus to add project semantics.

## Representation properties

“VMless,” “CPUless,” and “ABI-recovered” describe individual generated
implementations:

- a VMless implementation expresses original instruction effects in generated
  host code but may retain a CPU-shaped carrier;
- a CPUless implementation receives explicit values and services instead of
  interpreting instructions;
- an ABI-recovered implementation exposes an evidenced callable contract;
- any of these may remain DOS-memory-backed.

None implies that neighboring functions have the same representation.
Memoryless or native state is a separate property.

## Evidence and verification

Generation is not proof. A generated implementation descriptor should cite its
source identity, implementation digest, refusal or completeness information,
and verification evidence. Focused call verification can diagnose a candidate;
ReplayArtifact intervals provide reusable oracle-versus-candidate evidence for
the paths a replay covers. They create scoped verification claims, not a
timeless `verified` property. There is no global sample-count or coverage
threshold: development can proceed after a relevant passing interval, while
new corpus evidence and counterexamples continuously refine confidence.

Unknown transfers, runtime-code variants, unsupported effects, and unverified
paths remain explicit. They feed the same identity/evidence model used by the
[Execution Atlas](execution_atlas.md) and planner; they never become silent
fallbacks.

Current commands are listed in [the tool index](../tools/README.md). Historical
milestones and the original staged proposal are retained in
[the dos_re 2.0 lifting design](history/lifting_design_2.0.md).
