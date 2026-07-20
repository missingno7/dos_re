# Unified execution planner (dos_re 3.0)

Status: architecture and migration contract. This document defines the two
runtime dependency modes and the interfaces a future launch pipeline will
implement. It does not depend on the execution atlas being implemented.

## One program, two dependency modes

The recovered program has one stable identity and one override architecture.
There are only two top-level execution modes:

### Hybrid

The original executable remains available as an authority or runtime
dependency. Any reachable region may execute through:

- interpreted original code;
- a generated implementation;
- a selected faithful override;
- an optional enhancement or behavioral modification.

Hybrid is the development and verification environment. It supports mixed
execution, replay verification, oracle comparison, debugging, and gradual
replacement.

### Standalone

The original executable is forbidden at runtime. Every reachable region
required by the selected product profile has an implementation outside the
EXE, and every required runtime service is available without interpreter-only
or development-runtime dependencies.

A standalone product may mix generated VMless-style functions, generated
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
ports. No `play_memoryless.py` entrypoint exists; it is only a conceptual name.

| Current entrypoint family | Current ownership | Genuine distinction | Configuration or duplication |
|---|---|---|---|
| `play.py` | EXE boot, interpreter runtime, installed generated/manual hooks, snapshots, replay, verification flags, viewer, input, pacing, audio | Hybrid dependency mode; real-mode vs protected-mode device adapters | Override selection, baseline selection, verification role, and UI are currently combined. Several mature ports duplicate the shared player because their timing/viewer grew locally. |
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

Everything else should be expressed in one configuration and plan.

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

## Execution configuration

One configuration drives planning:

```text
ExecutionConfiguration
  program_identity
  dependency_mode          hybrid | standalone
  product_profile          roots, features, assets, package policy
  baseline_policy          ordered generated/interpreted provider preferences
  override_profile         selected OverrideSpec identities
  service_profile          platform and host service choices
  verification_policy      none | record | compare | category-specific
  packaging_target         development | desktop | executable | mobile | ...
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

Both identities are included in replay execution profiles and build reports.

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

## Launch API and thin presets

One API owns construction, planning, verification wiring, and launch:

```python
run_game(
    mode="hybrid",
    baseline_policy="interpreted-first",
    override_profile="faithful",
    product_profile="development",
)

run_game(
    mode="standalone",
    override_profile="release",
    product_profile="desktop",
)
```

`play_hybrid` and `play_native` may remain as user-facing thin presets:

```text
play_hybrid → mode=hybrid, development product profile
play_native → mode=standalone, release product profile
```

Internally the mode name is `standalone`; `play_native` does not promise that
every implementation is CPUless, ABI-recovered, or memoryless.

Real mode, protected mode, and detached products provide different execution
drivers and platform services behind the same launch pipeline. They are not
separate configuration authorities.

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

## Migration

1. Land the configuration, catalog protocols, plan, and report data contracts.
2. Adapt current IR/manifests/recovery facts into the temporary coverage-source
   interface; do not build an atlas inside this workstream.
3. Extract shared launch services—replay, input, pacing, display, audio,
   storage, and shutdown—from port runners into composable launch components.
4. Make current `play.py` and PM wrappers produce hybrid configurations.
5. Convert `play_hybrid.py` into a hybrid preset with a generated-first
   baseline policy and explicit interpreted frontier.
6. Convert current `play_vmless.py`, `play_cpuless.py`, and `play_native.py`
   implementations into provider/service factories consumed by standalone
   plans.
7. Produce detachment reports from current wall audits and runtime-closure
   tools; require a green report before standalone launch/package.
8. Keep only thin `play_hybrid` and `play_native` product presets where useful.
   Remove stage-named top-level runners and duplicated launch services.
9. When the atlas lands, replace the temporary coverage-source adapter without
   changing planner or launcher contracts.

Remove as top-level architecture:

- `play_vmless`, `play_cpuless`, and `play_memoryless`;
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
