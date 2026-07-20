# ReplayArtifact and cached continuation boundaries

dos_re has one primary replay artifact: a deterministic recording that drives
the original interpreter and a candidate implementation over the same stable
interval and compares their equivalent continuation state.

Earlier recording formats are unsupported. Downstream projects record new
artifacts through the current players. Design history is isolated in
[`history/replay_evolution.md`](history/replay_evolution.md).

## The verification model

```text
ReplayArtifact
  deterministic events + stable points + function visits
          │
          ├── oracle ReplayExecutionIdentity ─ continuation cache ─┐
          │                                                   ├─ canonical projection ─ compare
          └── candidate ReplayExecutionIdentity ─ continuation cache ┘
```

A `ReplayExecutionIdentity` is the immutable replay/cache identity of one
oracle or candidate execution composition:

- oracle or candidate role;
- the complete mixed implementation-plan digest;
- executable/lifted image;
- runtime and device model;
- continuation-state schema;
- canonical projection schema.

The implementation digest comes from the validated plan's bindings,
implementation descriptors, and executable runtime services. A mutable
backend hook table is not a second replay identity authority. Changing any
component rejects that identity's cache. Other execution
identities recorded under the same event stream remain usable.

This is distinct from the development, verification, detached, and release
policy profiles in `dos_re.execution`. A policy profile constrains allowed
capabilities; a `ReplayExecutionIdentity` keys the exact continuation semantics
of one selected mixed plan.

The capture profile is provenance, not an assertion of correctness. Interactive
capture may select previously replay-backed performance overrides or a provisional
candidate plan. An oracle capture is trusted immediately. A candidate capture
becomes trusted only after an equivalent `0 -> end_point` validation whose
candidate execution identity selects the same implementation, image, runtime,
devices, and schemas as the capture profile. Partial green intervals remain
useful verification records but do not promote the whole replay to evidence.
Replay trust means only that this finite event stream is oracle-backed. It
never certifies every function visited by the replay for unobserved inputs.

## Stable points and events

A replay chooses one canonical monotonic timeline. `ReplayPoint` is
`(timeline_id, ordinal)`; the pair is its stable identity. Function entry,
function exit, frame, instruction count, crash, divergence, and manual names
are annotations on a point, not competing clocks.

`ReplayEvent` stores a point, stable sequence number, channel, and canonical
JSON payload. The event-stream hash is part of the artifact. Drivers apply the
same events according to their own representation while stopping at exactly
the requested point.

The interactive players capture through `ReplayRecording`. It buffers
normalized immutable events, then finalizes exactly one `ReplayArtifact` with
the complete base continuation and optional cached endpoint. Real-mode
`replay_input.py` and protected-mode `pm_replay_input.py` are adapters only:
keyboard/mouse normalization and application, plus the PM stable frame seam.
They define no file names, manifests, versions, snapshots, or compatibility
branches.

Recording does not require an untouched interpreter plan. Capture composition
and device identity are retained so the same plan can be validated later.
Frontend metadata must also retain every deterministic knob needed to recreate
that topology.

## Continuation state is not comparison state

This separation is the central 3.0 rule.

### ContinuationState

Private to one replay execution identity. It contains:

- a schema identity;
- complete non-memory metadata;
- all byte-addressable regions needed for restore;
- the exact replay-event cursor.

It must include every field that can affect deterministic continuation: CPU or
native control state, timers, pending interrupts, devices, scheduler state,
open-resource positions, runtime state, and event cursor. The replay core does
not guess whether a runtime adapter is complete; the adapter's resume tests
must prove it.

### CanonicalState

The authoritative state compared between oracle and candidate. It contains a
shared schema identity, canonical JSON fields, and optional named byte regions.

- Interpreted, VMless, CPUless, and DOS-memory-backed profiles whose
  authoritative representation is identical can use `machine_projection` to
  compare complete machine metadata, regions, and event cursor.
- A detached native or memoryless profile projects its native objects into the
  same semantic schema as the oracle. Raw native layout is irrelevant.

A fully native selection is simply a mixed plan whose chosen implementations
have native properties and whose dependency closure excludes the lower-level
components it no longer needs. It uses the same replay corpus, points, function
identities, and verification operation as an early single-function candidate.

## Persistent boundaries

Each replay execution identity owns one base continuation state. Every cached
boundary stores:

- full continuation metadata and event cursor;
- only pages different from that execution identity's original base regions;
- page hashes and a complete reconstructed-state hash;
- the full replay-execution identity digest;
- the base continuation-state digest it was computed against.

Boundaries never form delta chains. Restoration always loads the identity's base
and applies one boundary's changed pages. The closest cached point at or before
X is restored, replay advances to X, X is cached lazily, and only X→Y executes.

The on-disk shape is:

```text
artifact/
  replay.json
  profiles/<profile-id-and-hash>/
    base/state.json
    base/regions/*.zlib
    boundaries/<point-key>/
      state.json
      pages/**/*.zlib
```

Artifact mutation uses a non-waiting artifact-local writer lock. A live second
writer is rejected, and a lock left by a dead local process is reclaimed.
Boundary publication is journaled in the top-level manifest before the staged
directory is atomically renamed. Opening the artifact completes a pending
rename, indexes an already-published valid directory, and discards an
unpublished staging directory. Unindexed derived cache directories are
discarded rather than interpreted as another artifact format. Paths are
contained under the artifact root; compressed payload sizes and hashes are
verified before use.

## Validation and invalidation

Opening an artifact verifies its format and canonical event-stream hash.
Accessing an execution identity additionally verifies the executable/generated image,
implementation, runtime, device model, continuation schema, projection schema,
and selected implementation digest. Restoring a boundary verifies the original
base-state digest, execution-identity digest, point identity, region sizes, page hashes,
and reconstructed continuation digest. A mismatch raises `StaleReplayError` or
`ReplayError`; stale data is never silently restored or repaired in place.

Invalidation is deliberately coarse and safe. Changing the event stream means
recording a new artifact. Changing a replay execution identity creates a new
cache namespace. Changing its base state invalidates every boundary in
that namespace. There is no partial cache migration.

## Differential verification

`verify_interval(artifact, oracle, candidate, X, Y)`:

1. validates artifact, event stream, timelines, and both replay execution identities;
2. restores each identity's nearest cache at or before X;
3. replays each identity exactly to X and compares their canonical state there;
4. rejects a non-equivalent X, otherwise lazily caches X for both identities;
5. replays each side only from X to Y;
6. projects and compares both endpoints in their shared canonical schema;
7. caches Y for both identities only when the endpoint is equivalent.

On mismatch, the already-diverged candidate endpoint is not cached as valid.
The artifact annotates X as the latest valid point before the observed
divergence.

Each equivalent result is retained as a scoped claim over the exact
implementation, oracle, replay, interval, and projection identities. No number
of such claims becomes an exhaustive proof. Projects normally use the first
relevant passing interval to keep development moving, accumulate further
claims as the corpus grows, and turn every later divergence into a permanent
focused regression.

## Divergence localization

`bisect_divergence` receives stable candidate stop points. It verifies cached
sub-intervals until it finds the smallest supplied transition whose endpoint
diverges. The final valid predecessor is cached and annotated with the
divergent successor and replay execution identities.

That persistent point is the reproduction reference. After a fix, tooling
restores it, tests the small transition, then verifies the function's full
first-entry to last-exit interval.

## Function visits and atlas identity

`FunctionVisitIndex` records per stable `FunctionIdentity`, independent of
whether the body came from a generated baseline backend or an authored
override:

- total invocation count, including recursive calls;
- point immediately before the first entry;
- point immediately after the final completed outermost exit.

Nested and recursive depth is tracked. An invocation still active when replay
ends marks the visit incomplete and invalidates interval verification, even if
an earlier invocation completed; no exit is fabricated for the active call.
These records become the execution atlas's inverse index from function to
covering replay artifacts.

Visit and transfer collection is independent of input capture. A project may
observe them inline when measured overhead is negligible, but the authoritative
path is replaying any existing artifact on the untouched oracle. Post-hoc
evidence records the exact oracle `ReplayExecutionIdentity`, event-stream hash,
observer identity/digest, and observed interval. Repeating the same recipe is
idempotent; different results under the same evidence identity are rejected as
non-deterministic.

Atlas ingestion accepts only a trusted artifact with oracle-produced execution
evidence. This preserves useful fast or provisional capture without allowing
its unverified observations to become program facts. Hooks can hide transfers
inside replaced bodies, so a post-hoc oracle run remains authoritative even
when lightweight inline observation is enabled.

## Runtime adapters

Each oracle or candidate supplies a `ReplayDriver`:

```python
class ReplayDriver:
    profile: ReplayExecutionIdentity
    current_point: ReplayPoint
    def capture() -> ContinuationState: ...
    def restore(state, point) -> None: ...
    def replay_to(artifact, point) -> None: ...
    def project() -> CanonicalState: ...
```

Game knowledge stays in adapters: event application, exact stop seams,
authoritative semantic fields, and stable lifted function identities. Storage,
validation, caching, interval selection, comparison, bisection, and divergence
annotations stay in `dos_re.replay`.

## Scope boundary

The replay artifact, real-mode and protected-mode continuation adapters,
interval verification, persistent boundary cache, divergence localization,
function-visit index, and optional oracle-owned observed-transfer section are
the shared infrastructure. `ExecutionAtlas.ingest_replay` consumes those
records by identity; Atlas storage, project-specific native object models, and
semantic projection fields remain independent of replay persistence.
