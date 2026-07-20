# Baseline execution and authored overrides (dos_re 3.0)

Status: workstream architecture and migration contract. The interfaces in this
document are the target design; existing modules named in the census remain
implementation inputs until their migration phase lands.

## Decision

dos_re has one original program, a catalog of reproducible baseline
implementation providers, one optional authored override layer, and one
execution planner:

```text
program identity + coverage source
              |
implementation catalog + override registry + runtime-service catalog
              |
execution planner (hybrid | standalone, product and verification profiles)
              |
immutable execution plan with per-function/region bindings
```

Interpreted original code, generated VMless code, and generated CPUless or
ABI-recovered code are alternative baseline implementations of the same
program. One plan may use different providers for different functions or
regions; they are not mutually exclusive product types. They are not patches,
recovery islands, or authored variants.
Generated output remains disposable and reproducible from the executable,
recovery IR, recovery facts, and toolchain.

There are two dependency-policy outcomes: **hybrid**, where the original EXE
may remain available, and **standalone**, where it is forbidden at runtime.
They are not different players. VMless, CPUless, ABI-recovered,
DOS-memory-backed, and memoryless are implementation or state-adapter
properties. The complete planning, reachability, readiness-report, universal
player, and export contract is defined in
[`execution_planner.md`](execution_planner.md).

All manually or AI-authored program code belongs to the override layer. An
override body is ordinary CPUless Python. It may use the DOS memory model when
that is the useful authoritative representation; CPUless does not imply
memoryless.

The override body is written once. Backend-specific adapters may marshal
registers, stack frames, memory, arguments, returns, virtual time, and control
flow, but those adapters do not contain a second semantic implementation.

## Repository census

The survey for this workstream found these current authorities:

| Current mechanism | What it owns today | Migration consequence |
|---|---|---|
| `dos_re.hooks.HookRegistry` | Real-mode handwritten `@registry.replace` registrations, names, installation, disabling, and oracle stripping | Its registration role moves to the unified override registry. Oracle purity becomes an execution-plan property. |
| `CPU8086.replacement_hooks` / `CPU386.replacement_hooks` | One shared dispatch dictionary for generated lifts, handwritten replacements, BIOS/environment services, replay clocks, boundary sentinels, verifier wrappers, and profiling wrappers | These dictionaries become backend-private dispatch machinery. Authored overrides, generated baseline dispatch, and infrastructure interceptors must not remain one public namespace. |
| `dos_re.lift.install` | Installs generated VMless modules into `replacement_hooks` and calls them replacements/hooks | It remains baseline assembly, but its terminology and output become a baseline implementation table rather than an authored override set. |
| `dos_re.lift.standalone.install_overrides` | CPUless module shadowing, retroactive imported-name rebinding, dynamic-dispatch cache invalidation, and restoration | Preserve the proven rebinding behavior as the CPUless backend adapter. Stop exposing it as a second registry authority. |
| `tools/cpuless_promote.py --overrides` | A separate JSON contract schema, virtual-time policy, generated CPU-ABI adapters, and composition seeding for handwritten CPUless bodies | Compile this information from the unified override declarations and shared function contracts. Remove the parallel input format after migration. |
| `dos_re.lift.emit_abi` proof-shadow loaders | Generated contract-proof substitutions implemented through the CPUless override stitch | Rename these generated substitutions. They are baseline verification projections, not authored overrides. |
| `dos_re.verification` / `dos_re.pm_verification` | Backend-specific hook differential transactions and temporary hook maps | Retain the proof engines behind the faithful-replacement verification policy. Their temporary interceptors remain infrastructure, not overrides. |
| `dos_re.hook_taxonomy` | Checkpoint, environment-wait, debug-probe, and glue roles | These are execution-point traits, not override categories. Move or rename them so the two classifications cannot be confused. |
| `dos_re.runtime_code` and signature guards | Identification of live code variants and runtime-installed bodies | This belongs to baseline code identity/version selection. SMC is not a behavioral modification merely because bytes change at runtime. |
| `player.py` hook modes and PM `install_hooks` callback | Port-defined selection, installation, verification modes, and replay-profile hashing | Replace with one execution configuration selecting dependency mode, provider policy, product profile, and override profile. Both players consume the same execution plan. |
| `bootstrap_lzexe`, `runtime_core`, `boundary_clock`, `pm_input_demo`, `frame_verify`, and profiling tools | Synthetic services, observation, parking, timing, or temporary wrapping installed at execution addresses | Route through a distinct interceptor/service channel. They are neither generated baseline functions nor authored program overrides. |

Generated and handwritten code are mixed today in two important ways:

1. VM-backed execution stores both in the same mutable `replacement_hooks`
   dictionary.
2. `cpuless_promote` lets a handwritten body suppress generation for one
   address while separately emitting its CPU adapter, but the declaration is
   not shared with the real-mode or PM hook registries.

Interpreter dependency is also duplicated. A traditional hook receives the
whole `CPU8086` or `CPU386` object and commonly performs its own register,
stack, memory, and return handling. The CPUless path instead expects a body
contract such as `func(mem[, platform], named arguments) -> outputs`. The
second shape is the correct semantic boundary; the first should become a
generated or framework-owned adapter.

### Terminology conflicts observed

The census found these words carrying incompatible meanings:

- **hook** names handwritten game logic, generated lifted functions,
  synthetic BIOS services, frame clocks, wait sentinels, verifiers, and
  profilers;
- **override** names both authoritative handwritten CPUless bodies and
  generated ABI contract-proof substitutions;
- **replacement** names a faithful authored implementation and any generated
  function installed instead of interpreter dispatch;
- **patch** names guest byte mutation, SMC operand materialization, and
  Python-level function replacement;
- **native** names VM-aware Python hooks, VMless generated code, CPUless code,
  DOS-memory-backed code, and detached memoryless products;
- **hybrid mode** combines a baseline choice, an override selection, and a
  verification/UI mode in one label.

The vocabulary below separates those axes. Existing source identifiers can
survive temporarily, but active architecture and new APIs use the precise
terms.

## Terms

The following vocabulary is normative for new work:

- **Original program**: the executable plus its observed runtime code
  identities.
- **Baseline implementation provider**: one reproducible way to execute an
  unchanged original function or region: interpreted, VMless generated, or
  CPUless/ABI-recovered generated.
- **Baseline implementation**: the generated implementation of one original
  function for a selected backend.
- **Override**: manually or AI-authored code intentionally selected instead of
  or alongside a baseline function at a stable target.
- **Backend adapter**: generated or framework-owned marshalling between a
  baseline backend and one override body.
- **Interceptor**: framework instrumentation or environment machinery such as
  tracing, verification, replay clocks, wait parking, profiling, or synthetic
  BIOS services. An interceptor is not an override.
- **Patch**: a mutation of guest code bytes or data. Do not use this word for
  Python function substitution.
- **Native**: never used without a qualifier. An override is CPUless by
  implementation and may still be DOS-memory-backed.

“Hook” may remain an implementation-level verb for dispatch interception, but
it is not an architectural layer or an override category.

## Stable target identities

Registry keys must not be Python module names or backend-local dictionary
keys. They are stable program identities shared with the recovery IR, replay
function visits, and the execution atlas.

The logical model has three parts:

- `ProgramIdentity`: identifies the original program/recovery-IR authority.
- `FunctionIdentity`: identifies a logical original function within that
  program.
- `ExecutionPointIdentity`: identifies a non-function point such as a verified
  checkpoint, wait seam, presentation boundary, or dispatch arrival.

For the current real-mode pipeline, a canonical recovery-IR `CS:IP` entry is a
valid function key inside one program identity. For flat protected mode, the
equivalent key is the recovery-IR linear entry. Symbolic names are display
metadata, not identity.

Addresses alone are insufficient for overlays and SMC. A target identity may
therefore carry a code-space or code-version discriminator. The versioned
code/SMC workstream will define its persisted representation; this workstream
requires only that registry APIs treat target identities as opaque,
serializable values rather than destructuring them as permanent `CS:IP`
tuples.

An override may target all code versions only when their recovered call
contract and behavior are proven equivalent. Otherwise each version requires
an explicit target declaration. Unknown live code identity fails loud.

The canonical serialized function identity is also the key recorded by
`FunctionVisitIndex` and, later, the atlas inverse index.

## Unified override registry

One declaration describes every authored override:

```text
OverrideSpec
  override_id          stable authored-code identity
  target               FunctionIdentity | ExecutionPointIdentity
  category             faithful | enhancement | behavioral
  implementation       importable CPUless callable identity
  contract_id          recovered invocation/return contract
  implementation_hash  source/build identity
  activation           explicit configuration/profile selector
  verification         category-specific policy metadata
  evidence             provenance and supporting tests/replays
```

The registry is declarative. It does not install directly into a CPU, mutate
`sys.modules`, choose providers, or choose a player mode. It validates
declarations and supplies them to the unified planner, which compiles an
immutable `ExecutionPlan` for a dependency mode, product profile, provider
policy, and override profile.

Selection rules are deliberately strict:

- A baseline function always exists independently of selected overrides.
- At most one selected replacement owns a target. Composition must be declared
  as one explicit composite rather than depending on registration order.
- Duplicate target ownership fails during plan construction.
- Activation is explicit; import order and decorators cannot change the plan.
- The ordered, hashed set of selected override specifications is part of the
  replay `ExecutionProfile`.
- A stale target, missing baseline counterpart, adapter-contract mismatch, or
  unsupported backend fails before execution.

The hook mechanism remains generic: target plus alternate implementation.
Category affects validation and verification policy, not registration or
dispatch architecture.

## Canonical override boundary

The handwritten callable receives natural arguments recovered for the
function plus a capability context where needed:

```text
implementation(context, argument_1, argument_2, ...) -> declared returns
```

The exact argument list is function-specific and comes from the shared
recovery contract. The common context is capability-based:

- mutable or read-only DOS memory, when the contract needs it;
- platform services explicitly required by the function;
- presentation or host-integration sinks when allowed by category;
- deterministic services explicitly present in the execution profile.

The context does not expose a CPU object, instruction stepping, decoder state,
or an implicit “fall back to ASM” operation. If a low-level function contract
still contains register-shaped values, its adapter supplies those as named
integers and receives explicit results. Stack and return mechanics stay in the
backend adapter.

This boundary permits an override to remain DOS-memory-backed for as long as
that is useful. Moving from raw offsets to views or detached objects is a
separate memory-model evolution, not a prerequisite for handwritten code.

Backend adapters implement the same invocation contract:

| Baseline backend | Adapter responsibility |
|---|---|
| Interpreted EXE | At the target, marshal registers/stack/memory into the recovered contract; invoke the body; apply outputs and exact continuation. |
| Generated VMless | Replace the generated function-table entry with an adapter using the same contract; do not edit emitted source. |
| Generated CPUless | Rebind static and dynamic calls to the body while retaining access to the generated baseline for oracle/delegation; preserve the proven late-binding/cache invalidation behavior. |
| ABI-recovered | Call the same body through the recovered public contract; generate a mechanical compatibility adapter only where a lower-stage caller still needs it. |

## Override categories and verification

### 1. Faithful replacement

A faithful replacement claims equivalent observable behavior to the original
function.

Requirements:

- The implementation is ordinary CPUless Python and contains no interpreter
  dependency.
- Its function contract, memory effects, return behavior, platform effects,
  and deterministic continuation effects are explicit.
- A carrier-backed adapter is differentially checked against the interpreted
  original where available.
- Replay verification covers the function's full first-entry to final
  completed last-exit interval.
- The endpoint comparison uses complete continuation state or the shared
  canonical semantic projection, as appropriate.
- Virtual-time or other backend-visible effects are modeled when they affect
  continuation. An inexact timing island cannot silently claim faithful
  equivalence under a timing-sensitive profile.

Failure of faithful verification is a defect, never an automatic
reclassification.

### 2. Non-authoritative enhancement

An enhancement intentionally changes presentation or host integration while
leaving authoritative game behavior unchanged.

The plan gives it a restricted context:

- authoritative game state is exposed read-only;
- presentation/host output is writable through explicit sinks;
- gamepad or host input integrations may emit normalized replay input events,
  but may not mutate game state behind the event stream;
- no mutable DOS memory or gameplay service is present unless a narrower
  read-only facade proves the operation harmless.

Verification compares runs with the enhancement disabled and enabled under the
same normalized inputs. Authoritative continuation or canonical state must
match. Only the explicitly owned presentation outputs are excluded from that
comparison. A neutral-mode parity check may additionally require original
pixels/audio, but intentional presentation output is not forced to match.

Read-only isolation is enforced by capabilities and tests, not a comment or
naming convention.

This category can be used once the required authoritative state seam is
verified. The surrounding game may still use interpreted, VMless, CPUless, or
DOS-memory-backed execution; the entire game need not first become memoryless.

### 3. Behavioral modification

A behavioral modification intentionally changes authoritative game behavior.
It must declare:

- its target and activation identity;
- the authoritative domains it is expected to change;
- its own unit, integration, and replay scenarios;
- any expected rejoin point or invariants that should still match the
  baseline;
- whether it is optional and how the unmodified plan is selected.

It is never run under a faithful-equivalence policy. The original oracle
remains useful as a baseline and for proving unchanged regions, but declared
differences are evaluated by modification-specific tests.

A declaration is not a broad diff mask. Divergence outside its named scope or
before activation remains a failure. The selected modification and its
declaration hash are part of the replay execution-profile identity.

## Baseline and override purity

Generated baseline directories carry generated-file banners and are
regenerated atomically. No authored module is emitted into those directories,
and no generator reads handwritten Python to infer the original program.

Override contracts may consume recovery-IR facts, but override source is not a
recovery fact and never changes what the baseline generator claims the
original program does.

The generated original body remains addressable for:

- faithful differential comparison;
- delegating enhancements that add presentation effects after baseline work;
- diagnosis and rollback;
- execution under an override-disabled profile.

Calling the generated body from an override must use a registry/backend
capability that cannot be rebound to the override itself.

## Replay and atlas integration

An `ExecutionProfile` identifies:

- dependency mode, execution-plan digest, and all selected implementation
  bindings;
- program and code-version identities;
- the complete selected override-plan digest;
- adapter/runtime/device and state-schema identities.

Changing any of these invalidates incompatible replay boundaries.

Function visits use stable `FunctionIdentity`, independent of which baseline
backend or override executed the body. The atlas can therefore answer:

- which replays cover the original function;
- which override profiles were active;
- the first-entry/final-exit verification interval;
- faithful pass/fail evidence;
- declared enhancement ownership;
- declared behavioral divergence scopes.

## Runtime code, overlays, and SMC

Runtime-written code is part of baseline identity until an authored override
is explicitly selected. `runtime_code.py` remains the evidence mechanism for
observed variants and installers.

The future versioned-code workstream should make these identities first-class
and let the baseline dispatcher select the observed generated variant. The
override registry then binds either the logical function or a declared
version-specific function identity. It must not guess from current bytes or
silently interpret an unknown variant.

## Migration map

Migration proceeds without changing all backends at once:

1. Introduce stable target, category, declaration, catalog, coverage-source,
   execution-configuration, execution-plan, and detachment-report types with
   no dispatch behavior change.
2. Compile current `HookRegistry` declarations into plans and move direct
   CPU-dictionary mutation behind real-mode and PM backend adapters.
3. Separate generated baseline dispatch from framework interceptors. Migrate
   BIOS services, replay clocks, parking sentinels, verifiers, and profilers
   to explicit service/interceptor channels.
4. Make `lift.install` publish baseline implementation tables rather than
   authored replacements.
5. Compile CPUless contracts and adapters from the same override declarations;
   retain the proven module-rebinding behavior behind that backend.
6. Migrate `cpuless_promote --overrides` consumers, then remove its separate
   JSON authority.
7. Route faithful verification through the existing hook oracles plus
   `ReplayArtifact` interval verification. Add enhancement isolation and
   behavioral-declaration tests.
8. Replace player hook modes and stage-named launch authorities with one
   profile-driven `play.py` and the unified planner. Real-mode, PM, and
   detached drivers consume the same plan model. Old entrypoints may remain
   temporary delegating aliases only.
9. Adapt existing graph manifests and recovery facts to the program-coverage
   interface. The future execution atlas can replace this adapter without
   changing the planner.
10. Remove the public legacy registration paths and update examples, tools,
    audits, terminology, and documentation.

Each phase must preserve an override-free execution plan and keep the original
oracle uncontaminated.

## Mechanisms to remove, retain, or rename

Remove after migration:

- public direct writes to `cpu.replacement_hooks`;
- decorator/import-order ownership of authored configuration;
- `DOS_RE_DISABLE_HOOKS` as the selection model;
- the standalone CPUless override registry as an independent authority;
- the separate `cpuless_promote --overrides` declaration format;
- player-specific `install_hooks` callbacks and the
  `--no-replacements`/`--safe-hooks`/`--trace-hooks` mode taxonomy;
- documentation that treats generated lifts as patches or requires authored
  bodies to be memoryless.

Retain behind new interfaces:

- real-mode and PM differential verifiers;
- exact call/return, stack, flags, and virtual-time adapters;
- CPUless imported-name rebinding and dynamic-dispatch cache invalidation;
- generated baseline installers and proof shadows;
- runtime-code variant evidence and fail-loud signature checks;
- replay profile hashing, function visits, and interval verification.

Rename or separate:

- generated “replacement hooks” to baseline implementations or backend
  dispatch entries;
- generated ABI “overrides” to proof substitutions/adapters;
- hook taxonomy to execution-point traits;
- “native hook” to the precise combination of override category and memory
  model.

`HookRegistry`, existing player flags, and direct CPU hook dictionaries may be
temporarily deprecated during migration, but there is no long-term
compatibility requirement once every in-repository consumer uses the new plan.

## Dependency order

The implementation order is:

```text
stable identities and terminology
        ↓
catalog/coverage protocols + declarative OverrideSpec
        ↓
ExecutionConfiguration + planner + immutable ExecutionPlan/DetachmentReport
        ↓
interpreter / PM / VMless backend adapters
        ↓
CPUless and ABI-recovered backend adapters
        ↓
category-specific verification policies
        ↓
universal player + closed-world exporter + project migration
        ↓
legacy registry and terminology removal
```

The registry and planner must not depend on a CPU backend. Verification
depends on the plan model, not the reverse. Replay and the future atlas consume
stable identities and execution-plan hashes without owning override dispatch.
The planner consumes an abstract coverage source; it does not depend on the
atlas implementation or storage schema.

## Non-goals for the first implementation slices

- No automatic semantic naming.
- No requirement to make the entire game memoryless.
- No requirement that a standalone product use one uniform recovery property.
- No implementation of the execution atlas in this workstream.
- No new source-reconstruction claim.
- No ordering semantics for multiple implementations at one target.
- No silent compatibility loader for project-local hook formats.
- No redesign of the replay verifier, lifting IR, or existing differential
  engines without evidence from adapter migration.
- No attempt to make instrumentation, environment services, and authored
  behavior one generalized callback system.

The first executable slice should prove one faithful replacement body runs
unchanged through at least the interpreted and CPUless baseline adapters and
is verified against one shared replay interval. Enhancement and behavioral
policies can then land independently on the same registry model.
