# One player, one execution planner, one export boundary (dos_re 3.0)

Status: architecture and migration contract, with the first executable slice
landed. `dos_re.execution` now provides immutable configuration, coverage,
implementation/service catalog, plan, and detachment-report contracts plus
the standard profiles and fail-loud planner. The real-mode `GameFrontend`
declares those inputs, resolves a plan before runtime construction, exposes
`play.py --profile ... --plan-only`, and includes the plan digest in replay
profile identity. PM/detached execution-driver migration, project alias
removal, and closed-world export remain later slices. This design does not
depend on the Execution Atlas being implemented.

## Decision

dos_re should have one permanent development entrypoint: `play.py`.

`play.py` resolves and runs any valid implementation mixture. It is not an
interpreter player, a VMless player, or a native player. Real mode, protected
mode, CPU-backed generated code, CPUless generated code, region overrides,
and detached state models are drivers, implementation providers, and services
behind one plan.

Standalone products are created by a separate `build` or `export` command.
The exporter resolves the same configuration, requires a closed-world plan,
freezes its digest, copies only the selected product-safe implementation and
service closure, and emits a minimal product launcher. The exported launcher
is not a second configuration authority and need not import the development
player or planner.

```text
project play.py --profile development|verification|detached|release
                         |
                one configuration + planner
                         |
                  immutable execution plan
                    /                \
       run in development tree      export --target ...
       with policy capabilities     closed-world product artifact
```

The recovered program has one stable identity and one override architecture.
Hybrid and standalone remain useful **dependency-policy outcomes**, not
players or launch modes:

### Hybrid dependency outcome

The original executable remains available as an authority or runtime
dependency. Any reachable region may execute through:

- interpreted original code;
- a generated implementation;
- a selected faithful override;
- an optional enhancement or behavioral modification.

Hybrid is normally the development and verification environment. It supports mixed
execution, replay verification, oracle comparison, debugging, and gradual
replacement.

### Standalone dependency outcome

The original executable is forbidden at runtime. Every reachable region
required by the selected product profile has an implementation outside the
EXE, and every required runtime service is available without interpreter-only
or development-runtime dependencies.

A standalone plan may mix generated VMless-style functions, generated
CPUless or ABI-recovered functions, DOS-memory-backed faithful overrides,
memoryless subsystems, coarse region replacements, enhancements, and
behavioral modifications. Uniform recovery level is not required.

```text
hybrid
= original executable available
+ generated implementation providers
+ selected override plan

standalone
= complete reachable implementation coverage
+ product-safe runtime services
+ selected override plan
- original executable and interpreter dependency
```

VMless, CPUless, ABI-recovered, DOS-memory-backed, and memoryless are
properties of implementations or state adapters. They are not product types,
dependency modes, or permanent top-level launch architectures.

## Entrypoint census

The workstream survey covered `dos_re` scaffolding and the active sibling
ports. No `play_memoryless.py` entrypoint exists; it is only a conceptual
name. None of the surveyed distinctions requires a permanent player.

| Current entrypoint family | Current ownership | Genuine distinction | Configuration or duplication |
|---|---|---|---|
| `play.py` | EXE boot, interpreter runtime, installed generated/manual hooks, snapshots, replay, verification flags, viewer, input, pacing, audio | It already proves one frontend can host interactive, headless, record, replay, and oracle/candidate workflows | Boot/provider selection, override selection, verification role, and UI are currently combined. It becomes the only project entrypoint, backed by explicit configuration. |
| `play_hybrid.py` | Generated VMless graph with a declared keep-interpreted frontier | None beyond hybrid reachability policy | It is `play.py` with a different generated provider set and strict frontier reporting. |
| `play_vmless.py` | EXE-independent boot image, generated VMless graph, interpreter poison, CPU carrier, DOS memory, product UI | Standalone dependency mode plus a VM-detachment proof property | Boot-image provider, generated implementation catalog, CPU-backed adapter, and wall audit are configuration/services—not a separate product architecture. |
| `play_cpuless.py` | CPU-free generated graph, platform scheduler, data image, import guards, viewer/input/audio, packaging closure; some ports also compose coarse handwritten region replacements | Standalone dependency mode plus CPU-detachment properties | Each port repeats launch, host, pacing, and product-service logic. CPUless is a provider/property, not the mode. |
| `play_native.py` | EXE-free handwritten subsystem or frame-level product, host frontend, assets, pacing, audio, menus, packaging | Standalone dependency mode | The name covers both DOS-memory-backed frame overrides and detached object models. Most differences from `play_cpuless` are selected implementation coverage and duplicated product services. |
| `play_memoryless.py` | No current implementation | None | Do not create it. Memorylessness is a region/state-adapter property reported by tooling. |

Concrete evidence from the active ports:

- Lemmings and SkyRoads `play_vmless.py` are already EXE-free standalone
  products despite retaining a CPU carrier and DOS memory.
- Lemmings, SkyRoads, and Overkill `play_cpuless.py` independently own
  standalone scheduling, import walls, input, display, and product bootstrap.
- Prehistorik 2 and Overkill `play_native.py` are large standalone product
  shells, but one uses recovered subsystems while the other centers a
  DOS-memory-backed frame/region implementation.
- The scaffold generator currently creates `play_oracle.py`,
  `play_hybrid.py`, and `play_vmless.py`, encoding recovery properties into
  entrypoint names.
- `dos_re.player` and `dos_re.pm_player` already centralize some hybrid
  services, but expose different construction and hook-installation seams.

The genuinely distinct runtime behavior is:

- original executable permitted versus forbidden;
- real-mode, protected-mode, or detached platform service implementations;
- product roots and reachability;
- state representation/adapters;
- available implementation providers;
- packaging constraints.

Everything else should be expressed in one configuration and plan. Real-mode,
protected-mode, and detached scheduling differences remain execution-driver or
runtime-service implementations; they do not justify separate user-facing
players.

## Implementation properties, not launch modes

Each available implementation declares capabilities and requirements:

```text
ImplementationDescriptor
  implementation_id
  target identities or covered region
  generated | authored
  faithful | enhancement | behavioral
  requires_original_exe
  requires_interpreter
  requires_cpu_carrier
  requires_dos_memory
  required_state_adapters
  required_runtime_services
  invocation/control-flow contract
  implementation digest and evidence
```

An interpreted implementation requires the EXE and interpreter. A generated
VMless implementation may require a CPU carrier and DOS memory without
requiring interpretation. A faithful override may be CPUless while still
requiring DOS memory. These are independent facts.

Large region replacements use the same descriptor. Their coverage claim names
the stable contained identities, entry/exit contract, externally reachable
edges, state ownership, and verification evidence. “This frame replaces most
of gameplay” is not a coverage declaration.

## Four orthogonal policy axes

One configuration drives planning, but keeps four concerns separate:

```text
ExecutionConfiguration
  program_identity
  composition              roots, provider preferences, selected overrides
  execution_policy         allowed dependencies and development capabilities
  verification_policy      none | record | compare | category-specific
  build_target             none | windows | mobile | other platform target
  product_profile          features, assets, services, package policy
```

The resolved forms remain distinct:

```text
CompositionPlan
  per-function and per-region implementation bindings
  state adapters, runtime services, unresolved frontier

ExecutionPolicy
  original_exe             required | allowed | forbidden
  interpreter_fallback     allowed | forbidden
  development_services     allowed capability set
  dynamic_loading          allowed | declared-only | forbidden

VerificationPolicy
  oracle profile and candidate profile
  replay corpus and intervals
  machine-state or canonical-state comparison
  category-specific exclusions and expected divergence

BuildTarget
  platform, architecture, package format
  product-safe import/service policy
  assets, licensing, signing, and launcher requirements
```

Execution composition answers what executes. Execution policy answers what may
be depended upon. Verification policy answers how correctness is evaluated.
The build target answers what is exported. No field silently implies another.

### Named profiles

Profiles are versioned configuration presets, not code paths:

| Profile | EXE/interpreter | Development capabilities | Purpose |
|---|---|---|---|
| `development` | allowed | replay record/playback, snapshots, diagnostics, instrumentation, profiling, experimental overrides | normal recovery work; unresolved nodes may bind to interpreted original code |
| `verification` | oracle required; candidate policy explicit | deterministic replay, cached boundaries, differential comparison, evidence recording | compare one candidate plan with the untouched oracle |
| `detached` | forbidden | selected diagnostics may remain, but cannot provide execution coverage | prove complete non-EXE execution closure in the development tree |
| `release` | forbidden | forbidden except explicitly product-safe services | validate the exact composition intended for export |

Projects may define additional presets, but presets only populate the
orthogonal axes. `detached` is deliberately separate from `release`: the first
proves executable closure; the second additionally proves product dependency
and packaging closure.

The former configuration fields map into these axes:

```text
  baseline_policy          -> composition provider preferences
  override_profile         selected OverrideSpec identities
  service_profile          composition plus execution-policy capabilities
  packaging_target         build_target
```

In hybrid mode the original executable is available. The selected baseline may
require it for some nodes or keep it only for oracle comparison.

In standalone mode the original executable and interpreter are forbidden. The
planner chooses among available generated implementations and selected
overrides per target; it does not require one global recovery level.

An override profile and a product profile are different:

- the override profile selects authored behavior;
- the product profile declares reachable roots, enabled product features,
  assets, and packaging requirements.

All identities are included in replay execution profiles and build reports.

## Planner inputs

The planner consumes three read-only catalogs:

1. **Program coverage source**
   - stable program, function, region, and execution-point identities;
   - product roots;
   - conservative reachable nodes and edges;
   - unresolved dynamic targets or unknown reachability.
2. **Implementation catalog**
   - generated implementations from IR/manifests;
   - selected override and region-replacement descriptors;
   - contracts, digests, and runtime requirements.
3. **Runtime service catalog**
   - device, scheduler, input, display, audio, storage, state-adapter, and
     platform implementations;
   - whether each service is product-safe or development-only;
   - packaging dependencies and supported targets.

The future execution atlas will implement the program-coverage-source
interface. This workstream does not import it or assume its storage schema.
Until the atlas lands, adapters can construct the same interface from recovery
IR, graph manifests, recovery facts, dispatch evidence, runtime-closure
measurements, and declared product roots.

The planner also publishes backend-neutral records the atlas may later
consume:

```text
PlanEvidenceSink
  record_plan_identity_and_bindings(...)
  record_unresolved_frontier(...)
  record_detachment_report(...)
  record_verification_evidence_reference(...)
```

This is an optional output protocol, not a planner dependency. Before the
atlas exists it may write a report file or do nothing. Atlas persistence,
queries, control-flow discovery, and replay inverse indexes remain in the
separate atlas workstream.

Unknown reachability is evidence, not absence. A standalone plan fails when a
required dynamic edge or product root cannot be resolved conservatively.

## Planning algorithm

For one configuration, the planner:

1. validates the program and product-profile identities;
2. obtains a conservative reachable region from the configured roots;
3. selects and validates the authored override plan;
4. binds each reachable function or region to one implementation;
5. applies explicit region coverage and checks external entry/exit edges;
6. gathers required state adapters and runtime services;
7. records unresolved nodes, edges, services, and representation frontiers;
8. constructs the verification plan independently of packaging;
9. computes the release import/resource closure for the packaging target;
10. returns an immutable `ExecutionPlan` and `DetachmentReport`.

Binding precedence is explicit, never import-order based:

```text
selected authored replacement or region owner
→ preferred compatible generated implementation
→ interpreted original implementation (hybrid only)
→ unresolved frontier
```

Enhancements that observe or replace non-authoritative output compose through
their declared presentation seam. They do not claim authoritative node
coverage merely by being selected.

Duplicate authoritative owners, incompatible state adapters, stale identities,
or unproven region edges fail during planning.

## Execution plan

The immutable result contains:

```text
ExecutionPlan
  program and configuration identities
  root set and reachability evidence
  per-node and per-region implementation bindings
  selected override declarations
  backend adapters
  runtime service bindings
  unresolved frontier
  verification plan
  package closure and exclusions
  plan digest
```

The launch pipeline executes this plan. Players do not discover hooks, import
generated modules opportunistically, or decide which fallback to use.

The plan digest is part of `ReplayArtifact` execution-profile identity.

## Detachment and standalone-readiness report

Every plan produces a report, including hybrid plans:

```text
DetachmentReport
  reachable nodes and reachability confidence
  generated coverage by implementation properties
  faithful override coverage
  region-replacement coverage
  enhancement and behavioral selections
  original-EXE-dependent nodes
  interpreter-only control-flow frontiers
  unavailable state adapters
  unresolved dynamic edges or execution points
  required runtime services
  development-only dos_re service dependencies
  packaging closure violations
  standalone_executable_ready
  package_ready
```

The human-facing report groups identities by:

- generated implementation;
- faithful function override;
- faithful region replacement;
- behavioral region replacement;
- original-only or interpreter-only;
- unresolved/unknown.

It also explains why each remaining development service is required and which
selected product root reaches it.

`standalone_executable_ready` is true only when:

- reachability is conservatively complete for the selected profile;
- every reachable authoritative node is covered outside the EXE;
- no selected path requires original code or interpretation;
- every required state adapter and runtime service is available;
- all fail-loud implementation frontiers are empty.

`package_ready` additionally requires the selected packaging target’s import,
asset, licensing, and development-runtime policy to pass. A program may be
executable without yet having a clean mobile or desktop package closure.

The report must never infer readiness from the name of a runner or from a
single successful playthrough.

## Universal player

One project entrypoint owns configuration loading, planning, and launch:

```python
run_game(
    profile="development",
)

run_game(
    profile="detached",
)
```

The corresponding CLI is one `play.py`:

```text
python scripts/play.py --profile development
python scripts/play.py --profile verification --replay artifacts/replays/...
python scripts/play.py --profile detached
python scripts/play.py --profile release
python scripts/play.py --profile detached --plan-only
```

`--profile release` is useful as a source-tree smoke run, but it does not
create a release artifact and is not packaging evidence.

Real mode, protected mode, and detached products provide different execution
drivers and platform services behind the same launch pipeline. They are not
separate configuration authorities.

The final architecture has no permanent `play_oracle`, `play_hybrid`,
`play_vmless`, `play_cpuless`, `play_native`, or `play_memoryless`. During
migration they may be temporary aliases which select a profile/provider
policy, print a deprecation warning, and delegate immediately to `play.py`.
They contain no runtime construction, service wiring, or product logic.

## Closed-world build and export

Export is a separate operation because running safely in the development tree
and proving that forbidden code is absent from an artifact are different
claims:

```text
dos-re build --profile release --target windows
dos-re export --profile release --target mobile
```

The exact command name can be chosen during implementation; there is one build
pipeline. It:

1. resolves the configuration and freezes the immutable plan plus digest;
2. requires conservative reachability to be complete from every product root;
3. rejects unresolved functions, region edges, dynamic targets, adapters, or
   services;
4. rejects EXE-backed and interpreter-backed bindings;
5. computes the transitive import, resource, native-library, and asset closure;
6. rejects development-only capabilities, imports, data, and dynamic discovery;
7. copies or compiles only selected product-safe implementations and services;
8. emits a minimal launcher bound to the frozen plan;
9. audits the finished artifact rather than trusting source configuration;
10. performs a clean-room smoke run where the target permits it.

The artifact must not contain the original EXE, interpreter, oracle drivers,
replay corpus or boundary cache, snapshot writers, profilers, plan discovery,
experimental overrides not selected by the plan, or diagnostic fallbacks.

Product-safe runtime components may initially retain a `dos_re` namespace if
they are explicitly selected and survive the closure audit. Package readiness
does not require cosmetic renaming or immediate memory-model detachment. It
does require that no development-only `dos_re` service is reachable or
packaged.

The exporter consumes the same frozen plan the development player can run. It
does not re-select providers during packaging. A plan digest mismatch,
post-plan source change, or undeclared dynamic import fails the export.

## Verification is not packaging

A standalone release excludes the original executable, interpreter, replay
cache, and verification-only bridges from its package closure. The same
standalone execution plan remains testable in the development repository:

- the original interpreted profile runs as oracle;
- retained `ReplayArtifact` intervals drive both profiles;
- complete machine state or canonical semantic state is compared;
- faithful overrides retain their per-function and interval proofs;
- enhancements compare authoritative state while excluding owned
  presentation output;
- behavioral modifications use their declared test policy.

Packaging success is not correctness evidence, and oracle availability in the
test environment is not a runtime dependency of the released product.

Conversely, verification success is not package-closure evidence. The
verification profile may legally load the EXE, interpreter, replays,
snapshots, canonical-state bridges, and diagnostics that the release profile
and exported artifact forbid.

## Migration

1. Land the configuration, catalog protocols, plan, and report data contracts.
2. Adapt current IR/manifests/recovery facts into the temporary coverage-source
   interface; do not build an atlas inside this workstream.
3. Extract shared launch services—replay, input, pacing, display, audio,
   storage, and shutdown—from port runners into composable launch components.
4. Make one project `play.py` load profiles and consume plans; adapt real-mode,
   protected-mode, and detached loops as drivers behind it.
5. Convert `play_oracle.py` and `play_hybrid.py` into temporary aliases
   selecting development profiles with interpreted-only or generated-first
   provider policies.
6. Convert current `play_vmless.py`, `play_cpuless.py`, and `play_native.py`
   implementations into provider/service factories consumed by standalone
   plans.
7. Produce detachment reports from current wall audits and runtime-closure
   tools; require a green report before detached/release launch.
8. Add the closed-world exporter and finished-artifact audit. Initially make
   `play_native.py` a release-profile alias, then remove it together with all
   stage-named aliases after project scripts and automation migrate.
9. When the atlas lands, replace the temporary coverage-source adapter without
   changing planner or launcher contracts.

Remove as top-level architecture:

- multiple permanent player entrypoints;
- `play_oracle` after its temporary alias period;
- `play_vmless`, `play_cpuless`, and `play_memoryless`;
- `play_hybrid` and `play_native` after their temporary alias period;
- runner naming based on a uniform recovery wall;
- generated-stage-specific viewer/input/pacing implementations;
- the assumption that complete standalone coverage requires one representation
  for the whole game.

Retain as diagnostics and implementation metadata:

- VM-detachment, CPU-detachment, ABI, memory-model, and package-closure audits;
- per-function/region implementation properties;
- import guards and fail-loud frontier checks;
- replay/oracle verification outside the release package.

The end state is a continuous replacement process:

```text
interpreted original
→ hybrid with selected generated implementations and overrides
→ increasing generated/function/region coverage
→ standalone-ready execution plan
→ independently packaged product
```

There is no architectural discontinuity at the point where the last
original-dependent reachable region gains an external implementation.

## Final invariants

1. One stable program identity and one implementation/override plan exist
   regardless of recovery mixture.
2. One `play.py` can execute every valid plan; drivers and services may differ,
   configuration authority may not.
3. Every reachable authoritative node or declared region has exactly one
   selected owner. Unknown or overlapping coverage fails loud.
4. Execution-policy restrictions are transitive. A forbidden dependency
   cannot return through an adapter, dynamic import, service, or fallback.
5. The detached and release profiles forbid the EXE and interpreter; they do
   not merely omit command-line paths to them.
6. A release plan is closed-world and reproducible. Export binds the exact
   plan digest and audits the finished artifact.
7. Verification and packaging remain independent claims over the same
   composition.
8. Recovery-level properties are reported per implementation or region, never
   inferred from a player/profile name.
9. Development-only evidence and tools may inspect release composition but
   cannot enter its transitive artifact closure.
10. The planner depends only on stable identity, coverage, implementation, and
    service protocols—not on Execution Atlas persistence.
