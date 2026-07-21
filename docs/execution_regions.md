# Long-lived execution regions

An execution region is a selected implementation that owns control for more
than one original call boundary. It is the mechanism for replacing a complete
subsystem without making a second player or preserving hooks between functions
that the subsystem now owns internally.

```text
surrounding carrier ── declared entry ──> active RegionSession
                                            │
                                  semantic replay yields
                                            │
surrounding continuation <── declared exit ┘
```

The surrounding carrier can be interpreted or generated. The region can use a
different carrier and state representation. Recovery level remains a property
of the selected implementation, not a global mode.

## Implemented contract

`ExecutionRegionContract` is attached to the region's ordinary
`ImplementationDescriptor`. It declares:

- one stable region identity;
- the surrounding carrier through which it is entered;
- the region's own carrier;
- `RegionEntryPoint` records mapping named entries to stable program points;
- `RegionExitPoint` records mapping named outcomes to stable continuations;
- every original target whose runtime ownership becomes internal to the
  region;
- the authoritative-state ownership mode;
- semantic replay boundaries;
- state inputs and outputs used by verification and future state bridges.

The supported ownership modes are shared DOS memory, native-owned state, and
imported native state. Shared memory is the safest first migration because the
surrounding generated code and the authored region observe one authoritative
image. Native-owned modes require explicit project adapters at the external
boundary; the generic dispatcher never guesses a conversion.

`RegionAdapter` is the bridge exposed by a root or surrounding provider. It
names the exact host carrier, region carrier, activation callable, and content
digest. Selection fails if the chosen root carrier does not expose a bridge for
the region. Importing a region module cannot activate it.

The planner emits one `ResolvedExecutionRegion` for every selected region. It
contains the exact adapter and digest, entry/exit graph, ownership mode,
replay boundaries, covered targets, and the ordinary bindings made dormant
while the region owns control. The plan digest covers all of these fields.

## Contextual ownership and boundary collapse

A target covered by a selected region still has an ordinary plan binding. That
binding remains valid whenever control is outside the region and is useful for
oracle execution, diagnostics, and independent function verification. While
the region is active, however, the region owns those targets contextually and
the inner bindings are dormant.

This is suppression, not deletion:

- original function and program-point identities remain available to the
  Atlas, replay evidence, and differential tooling;
- the runtime does not cross an adapter at each historical call;
- only declared entry and exit edges remain active seams;
- disabling the region exposes the pre-existing generated or interpreted
  bindings again through a newly resolved plan.

Planning rejects partial claims, undeclared covered targets, and overlapping
selected regions. A provider cannot hide an unresolved target merely by
assigning it a region label.

## Runtime lifecycle

`RegionDispatcher` owns at most one active `RegionSession`. A project adapter
creates the session at a declared entry and unwinds the surrounding carrier
with `RegionHandoff`. The canonical frame driver then calls `advance()` until
the session either:

- yields one declared semantic replay boundary; or
- returns one declared exit outcome.

On exit, the project adapter applies the declared state export, if any, and
restores the mapped continuation. Unknown entries, yields, exits, nested
handoffs, and excessive same-frame transfers fail loudly. The generic runtime
does not know registers, stacks, DOS layouts, native objects, game timing, or
presentation rules; those belong to the carrier-to-region adapter.

Function hooks remain appropriate for small replacements. A long-lived region
is appropriate once the subsystem has coherent lifecycle and state ownership.
The two use the same catalog and plan; regions are not a second hook registry.

## Replay and verification

One `ReplayArtifact` timeline spans all carriers. A region session yields the
same backend-independent boundary identity used by the surrounding player,
such as a completed game tick or input wait. Guest-instruction coordinates can
remain diagnostic metadata but are not a required contract for authored
semantic code.

Region verification uses an explicit two-surface contract (see
[`verification_contracts.md`](verification_contracts.md)):

1. replay the original oracle to the declared entry;
2. run the selected region across one or more semantic yields;
3. compare the declared canonical semantic state and observable interval
   evidence while the region owns control;
4. compare the declared continuation seam at every exit into surrounding code;
5. refine a failed interval to detailed point or instruction evidence.

Shared-memory regions commonly use complete continuation comparison at their
external seams. Detached native-state regions declare canonical semantic state
inside the island and reconstruct only the receiving continuation contract at
an external seam. A finite green corpus is scoped evidence, not proof over
inputs the corpus never exercised.

## Whole-program providers

A generated whole-program provider is replaceable only when it exposes stable
internal entry points and `RegionAdapter` bridges. Its frame driver must honor
the immutable region bindings and resume the declared continuation after exit.
An opaque direct-call graph that bypasses the plan is not a valid mixed
provider.

This permits one resolved graph to contain, for example, a generated VMless
frontend, an ABI-recovered loader, and authored native gameplay. Generated
coverage can make that graph EXE-free before every region is authored native.

## Release materialization

Materialized execution-plan schema `dos_re.execution-plan/v3` records the
resolved region graph alongside function bindings, services, features, and
bootstrap. It includes only selected adapter identities and digests. Release
export therefore has enough information to generate static handoff tables or
calls without running the development planner or packaging unselected
implementations.

The normal dependency closure still decides whether CPU, DOS memory, DOS
services, or dos_re itself remain. Moving a region from shared DOS memory to
native-owned state can remove dependencies independently. When the surrounding
regions are replaced, the external handoffs move outward. When one final owner
covers the whole reachable graph, those seams disappear naturally.

## SkyRoads vertical slice

SkyRoads is the first executable pilot:

```text
generated VMless menu
    → stable gameplay entry 1010:2317
authored skyroads.gameplay RegionSession over shared DOS memory
    → level-completed or player-died
generated continuation 1010:20AD
```

The original parallel native player proved the authored gameplay work was
valuable, but it independently approximated missing menus and transitions.
The region migration keeps that code inside the canonical program instead:
the generated frontend remains authoritative, the native session owns the
gameplay loop across many ticks, and the generated graph resumes the original
continuation. Internal gameplay hook seams are dormant while the session is
active.

## Current limits

- The dispatcher deliberately permits one active region; nested regions need
  an explicit ownership and unwind design rather than accidental recursion.
- Native-state import/export codecs are declared by the contract but remain
  project work for the first region that leaves shared memory.
- The materialized graph is ready for static product binding; individual ports
  still provide their target-specific generator or launcher.
- Atlas enrichment retains the historical inner identities but does not select
  regions or replace planner validation.

These limits preserve one authority at each layer while allowing larger
regions to land independently.
