# Progressive replacement and hook-boundary collapse

dos_re recovers one program through a changing implementation graph. A hook is
not a recovery level or a permanent architecture: it is an adapter at an edge
whose source and target are currently owned by different selected
implementations. When one implementation later owns both endpoints, the
planner reports that edge as collapsed and the runtime seam disappears.

## Final model

```text
conservative reachable identities + known transfers
                         |
          ImplementationCatalog candidates
                         |
 ordered composition preference + execution/evidence policy
                         |
                 immutable ExecutionPlan
        selected owner per target + one root carrier
                         |
        active cross-owner edges / collapsed edges
                         |
 development binding                    release materialization
 through adapters                       (no runtime planner)
```

A plan may contain interpreted regions, generated VMless functions, generated
CPUless or ABI-recovered regions, authored faithful subsystems, presentation
features, and intentional modifications simultaneously. These are properties
of candidates. They are never global player modes.

Complete generated VMless coverage can make a product EXE-free even while it
retains a CPU-shaped carrier and many recovery seams. Replacing larger regions
with authored or ABI-recovered implementations improves the same product. It
does not start a second port.

## Separate concepts

- An **implementation** owns original-program semantics for stable targets.
- A **provider** is an implementation that owns a region or program root and
  supplies its internal execution graph.
- A **carrier** is the surrounding calling/state mechanism, such as
  `interpreted-cpu`, `generated-vmless-cpu`, `generated-cpuless`, `dos-memory`,
  or `native-state`.
- An **adapter** marshals one backend-neutral implementation contract through
  one carrier. It is a seam, not another semantic body.
- A **composition** is ordered implementation preference plus explicit authored
  enablement. It is planner input, not executable code.
- A **feature** is optional product behavior, presentation, or instrumentation;
  it does not own recovered program coverage.
- An **overlay** is a presentation/input surface for feature state. It does not
  mutate implementation selection.
- The **oracle** is a development verification dependency, not a fallback.
- A **release** is a materialized selected graph and dependency closure, not a
  recovery level.

## Candidate declarations

`ImplementationDescriptor` declares stable targets, origin, `RecoveryLevel`,
dependency requirements, finite `EvidenceGrade`, content digest, optional
`ImplementationContract`, and an intrinsic carrier for region providers.
`ImplementationEntry` holds the semantic callable and explicit `BackendAdapter`
records. Each adapter has its own stable identity, carrier, and digest.

`EvidenceGrade.REPLAY_CORPUS` means only that the cited finite corpus passed.
It is not universal correctness. Development policy may permit focused or
provisional candidates. Product policy can require stronger evidence for
selected authored candidates, and the candidate-resolution report explains an
evidence rejection and the exact fallback selected instead.

The same semantic callable can therefore be selected through an interpreted
CPU adapter, a generated-VMless CPU adapter, a recovered ABI adapter, or a
native-state adapter. Adapters may marshal arguments, registers, DOS memory,
native state, effects, returns, continuation, and explicit virtual-time yields;
they must not duplicate the implementation's semantics.

## Boundary collapse

`ProgramCoverage.edges` carries known resolved or observed transfers. The
Atlas supplies these edges when available, but planning also accepts another
conservative `CoverageSource`. For every known edge the plan compares selected
ownership:

- equal owner: the edge is internal and counted as collapsed;
- different owner: the report emits an `ExecutionBoundary`, including the
  active carrier and target adapter identity;
- unresolved transfer: detached and release policy still fail conservatively.

This makes collapse measurable. A leaf replacement creates seams around a
small island. A subsystem provider claiming the verified contained targets
removes internal seams, leaving only its evidenced entries and exits. A whole
program provider collapses every known internal implementation edge. Unknown
edges never disappear merely because a region has a descriptive label.

Generated whole-program providers must expose replaceable stable targets. A
provider that bypasses selected inner bindings is not a valid mixed
composition. During development it may use plan-driven dispatch or generated
binding tables. Export may statically bind or regenerate calls. Either way,
the selected graph—not source import order—is authoritative.

## Product features

`FeatureCatalog` is separate from implementation ownership. Feature categories
are presentation, behavioral, and instrumentation. A presentation feature is
declared read-only with respect to authoritative state. A behavioral feature
must declare authoritative divergence, a replay event channel, and safe
application boundaries. `FeatureController` records live behavioral changes
and queues both live and replayed changes until those boundaries. This covers
events such as invulnerability, level switching, renderer selection, or
widescreen state without contaminating faithful oracle claims.

Faithful differential verification rejects selected behavioral implementations
and enabled behavioral features. Modification-specific tests use a different
verification policy. Instrumentation remains a development dependency and is
excluded by the standard release policy.

## Development and release

Development keeps the catalog, planner, oracle, replays, diagnostics, and
multiple candidates. The runtime consumes the chosen carrier and exact
bindings. It cannot silently call an implementation lacking a carrier adapter.

Release export writes `execution_plan.json` beside the release manifest. It
contains the final carrier, target bindings, selected implementation and
adapter digests, features, services, and bootstrap provider. Product build
code can consume this plain JSON to generate static calls or dispatch tables;
the planner and unselected candidates are not runtime dependencies. Export
still proves the closed file/capability closure and rejects interpreted or EXE
fallback when policy forbids them.

## Validation failures

Planning or declaration fails for:

- conflicting authored owners for one authoritative target;
- an enabled candidate below the product's finite evidence policy, with no
  permitted fallback;
- a selected implementation without an adapter for the root carrier;
- multiple program roots selecting incompatible carriers;
- presentation features that claim authoritative mutation;
- behavioral features without a replay channel or safe boundary;
- faithful verification with behavioral divergence enabled;
- unresolved release coverage, forbidden dependencies, missing bootstrap, or
  an incomplete export closure.

Ports should additionally test that production candidates are reachable in a
real plan, generated providers honor inner bindings, feature events round-trip
through replay, and a packaged launcher consumes only the materialized plan.

## Migration map

| Earlier state | dos_re 3.0 destination |
|---|---|
| hook registry or launch flag selects behavior | catalog entry plus configuration preference |
| separate interpreted/VMless/CPUless/native player | one player; provider choice determines root carrier |
| handwritten body duplicated per backend | one `ImplementationContract` and semantic body, several carrier adapters |
| generated whole-program graph is opaque | stable internal targets plus plan-driven or materialized bindings |
| `verified=True` | finite evidence grade and cited evidence identities |
| hook count inferred from registration | `ExecutionBoundary` graph from coverage transfers and selected ownership |
| cheats or renderer switches mutate globals | planned feature state and replay events at safe boundaries |
| release imports planner and chooses again | exported `execution_plan.json` with one closed selection |
| completed native script beside the workbench | progressively detached product; dos_re may disappear from its final closure |

SkyRoads is the first executable pilot. Its existing CPU-backed authored bodies
must bind through both interpreted and generated-VMless carriers, its generated
provider must remain the fallback when authored bodies are disabled, and its
plan report must expose the mixed ownership/boundary graph. The historical
`pre2_port` is evidence for the desired destination—large native islands,
native state ownership, and product features—not an implementation to copy.

## Remaining milestones

This workstream establishes the general authorities; it does not label dormant
native code as production-ready.

1. **Current framework:** carrier-aware candidate selection, finite evidence
   policy, edge-derived boundary reporting, replayable feature state, and
   closed-world plan materialization are implemented in dos_re.
2. **Current SkyRoads pilot:** authored faithful bodies bind through interpreted
   and generated-VMless carriers; the VMless provider is the clean fallback;
   the Atlas drives a measurable seam graph; one behavioral feature round-trips
   through the replay adapter.
3. **Generated CPUless binding:** emit or materialize stable internal call
   bindings so authored ABI candidates can replace targets inside a generated
   whole-program graph without dynamic planner imports.
4. **Larger subsystem providers:** promote only evidenced native assemblies,
   declare their contained identities and external contracts, and watch their
   internal boundaries collapse in the same report.
5. **State ownership:** add DOS-memory and native-state adapters for those same
   bodies, then detach CPU, DOS memory, DOS services, and dos_re independently
   as the selected dependency closure permits.
6. **Product surface:** drive presentation enhancements and behavioral options
   from the common feature controller/overlay contract, with authoritative
   writes restricted to declared behavioral features.
7. **Standalone product:** consume the materialized plan to generate static
   bindings, package only the selected graph, pass the hermetic corpus, and
   finally remove dos_re from the product closure. dos_re remains the recovery
   workbench and oracle environment.
