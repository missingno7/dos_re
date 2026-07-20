# Unified baseline and override architecture (dos_re 3.0)

Status: implemented declaration and selection model.

dos_re has one original program, one implementation catalog, and one execution
plan. Generated implementations reproduce unchanged original behavior.
Manually or AI-authored behavior is declared separately and selected
explicitly by configuration.

## Stable targets

Catalog targets are stable `ProgramIdentity`, `FunctionIdentity`,
`RegionIdentity`, or `ExecutionPointIdentity` values shared by recovery IR,
replay function visits, plans, and the Execution Atlas. Symbol names
are display metadata.

Addresses can seed identities, but identities remain opaque so the
versioned-code workstream can add overlay or SMC discriminators. An
implementation may cover multiple code versions only when their contracts are
proven equivalent.

## One catalog

`ImplementationCatalog` contains immutable `ImplementationEntry` records:

```text
ImplementationEntry
  descriptor
    implementation_id
    stable targets / region
    interpreted | generated | authored
    baseline | faithful | enhancement | behavioral
    implementation properties
    dependency-capability requirements
    runtime-service requirements
    required assets / supported build platforms
    verification evidence
    implementation digest
  implementation       semantic callable when applicable
  activate             backend binding adapter
```

There is no global authored registry, import-time registration, environment
selection, or runtime installation side channel. Authored implementations are
inactive unless their identities appear in the execution configuration.
Duplicate identities and conflicting selected owners fail during planning.

Generated code remains disposable and reproducible. It never contains
project-authored semantics. Backend-private dispatch dictionaries may still
exist inside CPU or generated runtimes, but only a plan activator may populate
program implementation entries.

Framework interceptors—replay clocks, verification wrappers, wait parking,
profilers, and synthetic environment services—are runtime services, not
program overrides.

## Canonical authored boundary

An authored semantic callable is ordinary CPUless Python:

```text
implementation(context, recovered arguments...) -> declared results
```

It never receives an instruction decoder or an implicit fallback-to-ASM
operation. Its capability context contains only declared memory views,
platform services, deterministic services, and presentation sinks.

The `activate` callable is backend-specific. It maps stable targets and the
recovered contract to registers, stack, memory, arguments, returns, timing,
and control flow. The semantic implementation is not duplicated across
interpreted, generated VMless, generated CPUless, or ABI-recovered callers.

CPUless does not imply memoryless. A faithful implementation may continue to
use the DOS memory model when that is the useful authoritative state.

## Categories

### Faithful replacement

A readable or faster implementation claiming equivalent observable behavior.
It must declare its contract, effects, continuation, services, and digest.
Replay verification covers the complete first-entry through final completed
last-exit interval and compares complete continuation or canonical semantic
state. A mismatch is a defect, not an automatic reclassification.

### Non-authoritative enhancement

Presentation or host integration which intentionally changes owned output but
not authoritative gameplay. Its context exposes authoritative state read-only
and writable presentation sinks. Verification compares authoritative state
with the enhancement enabled and disabled, excluding only declared output.

Enhancements attach to a seam; they do not claim authoritative function
coverage.

### Behavioral modification

An intentional authoritative change. It declares activation, affected
domains, tests, expected invariants or rejoin points, and divergence scope.
It never runs under faithful-equivalence policy. Differences outside the
declared scope remain failures.

## Backend portability

Backend activators implement the same recovered contract:

| Caller | Activator responsibility |
|---|---|
| interpreted original | marshal machine state, call the body, apply results and exact continuation |
| generated VMless | replace the selected generated dispatch entry without editing emitted source |
| generated CPUless | bind static and dynamic calls while retaining generated baseline evidence |
| ABI-recovered | call the shared body through the recovered public contract |
| detached region | connect declared region entry/exit edges and state ownership |

Large region replacements use the same catalog. Their descriptor must name
contained stable identities, externally reachable edges, state ownership,
services, and verification evidence. A descriptive claim such as “most of
gameplay” is not coverage.

## Verification and replay identity

The execution-plan digest includes selected implementations, descriptors,
services, coverage evidence, policy, and build target. Replay cache identity
therefore changes whenever implementation selection or its evidence changes.

The original interpreted plan remains the oracle. Candidate plans may mix
generated and authored implementations at any recovery level. Complete
machine projection is used while representations match; canonical semantic
projection supports detached native state.

## Runtime code and SMC

Runtime-written code remains part of original-program identity until a
catalog implementation is selected. `runtime_code.py` records observed code
variants and fail-loud signatures. Unknown live variants never silently
interpret or select an authored body.

## Dependency order

```text
stable identities and recovered contracts
-> ProgramCoverage and ImplementationCatalog
-> explicit ExecutionConfiguration
-> immutable ExecutionPlan and DetachmentReport
-> backend activators and runtime services
-> ReplayArtifact verification evidence
-> closed-world release export
```

The catalog and planner import no CPU backend. Verification consumes plans;
it does not own implementation selection. The Execution Atlas implements and
consumes stable protocols without owning dispatch.
