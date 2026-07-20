# Unified execution lifecycle (dos_re 3.0)

Status: implemented architecture.

dos_re has one recovered-program identity, one coverage model, one
implementation catalog, one execution configuration, one planner, one player
pipeline, and one closed-world exporter. Recovery levels describe individual
implementations; they never select a different player.

## Lifecycle

```text
ProgramCoverage + ImplementationCatalog + RuntimeServiceCatalog
                              |
                    ExecutionConfiguration
                              |
                         plan_execution
                              |
                 ExecutionPlan + DetachmentReport
                    /                       \
       player.main(frontend)          tools/export.py
       development tree               closed-world artifact
```

The player resolves the plan before constructing a runtime. A strict policy
therefore fails before an EXE loader, interpreter fallback, unavailable
service, or unresolved implementation can be used.

Real-mode and protected-mode behavior are frontend drivers behind
`player.main`. `PMFrontend` owns PM devices and presentation but has no parser
or second entrypoint.

## Configuration axes

`ExecutionConfiguration` separates:

- composition: product roots, provider preference, and selected authored
  implementations;
- execution policy: whether the original EXE, interpreter, development
  capabilities, and dynamic loading are allowed;
- verification policy: oracle requirement and comparison mode;
- product profile: reachable roots, features, services, and assets;
- build target: platform and package format.

The standard profiles are:

| Profile | EXE/interpreter | Development services | Purpose |
|---|---|---|---|
| `development` | allowed | replay, snapshots, diagnostics, profiling, instrumentation | recovery work with an explicit interpreted frontier |
| `verification` | allowed for the oracle | replay, snapshots, diagnostics, instrumentation | compare a candidate plan against the untouched oracle |
| `detached` | forbidden | only explicitly allowed diagnostic services | prove complete non-EXE execution closure |
| `release` | forbidden | forbidden | prove package closure and export the product |

Profiles only populate policies. VMless, CPUless, ABI-recovered,
DOS-memory-backed, and memoryless remain descriptor properties.

## Coverage and identities

`ProgramCoverage` contains stable roots, conservative reachable identities,
unresolved edges, and an evidence identity. It implements the `CoverageSource`
protocol directly. Recovery-IR adapters can construct it now; the future
Execution Atlas can implement the same protocol without changing the planner.

Unknown reachability is not absence. Detached and release planning reject
unresolved edges.

The planner emits backend-neutral plan bindings, frontier information,
detachment evidence, and verification references which the future atlas may
consume. Atlas persistence is not a planner dependency.

## Implementation catalog

`ImplementationCatalog` is the only authority for executable program
implementations. Every `ImplementationEntry` contains:

- an immutable `ImplementationDescriptor`;
- the semantic callable, when authored code exists;
- a backend activator which binds the selected stable targets to a runtime.

Descriptors declare origin, override category, covered targets or region,
recovery properties, EXE/interpreter requirements, required runtime services,
region identity, and implementation digest.

Authored entries are inactive unless named by `selected_overrides`. Duplicate
implementation identities or multiple selected authored owners fail during
planning. Runtime construction performs no global registration and import
order cannot select behavior.

Binding precedence is deterministic:

```text
explicitly selected authored implementation
-> preferred compatible generated implementation
-> interpreted original implementation when policy permits it
-> unresolved frontier
```

Enhancements remain non-authoritative attachments and cannot silently claim
authoritative coverage. Faithful and behavioral implementations carry their
category into verification policy.

## Runtime services

`RuntimeServiceCatalog` is the sole service authority. The planner computes
the transitive service closure, rejects missing services, checks development
capabilities against execution policy, and distinguishes product-safe from
development-only services.

Real-mode, protected-mode, detached scheduling, display, input, audio,
storage, state adapters, and host integration are services or frontend
drivers—not player architectures.

## Detachment report

Every plan contains one `DetachmentReport` listing:

- reachable identities and selected bindings;
- generated, faithful, and region-replacement coverage;
- original-EXE and interpreter dependencies;
- unresolved identities and control-flow edges;
- required, missing, development-only, and policy-forbidden services;
- standalone-executable readiness;
- package readiness.

`play.py --profile detached --plan-only` prints the report without booting.
A successful playthrough or a filename is never readiness evidence.

Standalone-executable readiness requires complete conservative coverage,
no EXE/interpreter binding, no missing service, and no unresolved frontier.
Package readiness additionally requires a build target and a product-safe
service closure.

## Replay and verification

The immutable plan digest covers policy, coverage, bindings, descriptor
metadata and digests, selected services, and build target. It participates in
`ReplayArtifact` execution-profile identity, so stale boundaries cannot cross
execution plans.

Verification is independent from packaging:

- the verification environment may retain the EXE, interpreter, replay
  corpus, cached boundaries, snapshot machinery, and canonical-state bridges;
- the release artifact contains none of those development authorities;
- both sides refer to the same stable program identities and candidate plan;
- faithful implementations compare complete machine state or canonical
  semantic state;
- enhancements exclude only their declared presentation outputs;
- behavioral modifications use declared tests and divergence scopes.

## Closed-world export

`tools/export.py --factory MODULE:CALLABLE --output DIST` consumes the exact
package-ready release plan. The factory returns the plan, explicit `ExportFile`
closure, and launcher path.

The exporter:

1. accepts only a `release` plan whose report is package-ready;
2. requires an explicit file list and refuses directory discovery;
3. rejects duplicate or escaping destinations;
4. statically rejects imports of the EXE/interpreter, planner, players,
   replay, snapshots, and verification machinery;
5. copies into a private staging directory;
6. hashes every finished file;
7. writes `dos_re_release.json` with the plan digest, target, launcher, and
   file hashes;
8. atomically publishes the finished directory and never overwrites one.

Product-safe runtime components may retain a `dos_re` namespace. Readiness is
about dependency closure, not cosmetic naming or a uniform memory model.

## Invariants

1. Every reachable authoritative identity has exactly one selected owner.
2. Import order, environment variables, and runtime hook flags cannot select
   behavior.
3. Forbidden dependencies cannot return through an adapter, service, dynamic
   import, or fallback.
4. Every non-interpreted selected provider requires an explicit backend
   activator.
5. Real-mode and PM execution share one parser, configuration, planner, and
   dispatch lifecycle.
6. Detached and release policies forbid the EXE and interpreter; omission by
   convention is insufficient.
7. Export binds and audits the exact plan rather than re-planning.
8. Verification success and package closure are separate claims over the same
   composition.
9. Recovery properties are reported per implementation or region.
10. The Execution Atlas can implement or consume the stable protocols but is
    not required by this lifecycle.
