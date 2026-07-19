# Deterministic replay and continuation state (dos_re 3.0)

dos_re has one primary replay artifact: a deterministic recording that drives
the original interpreter and a candidate implementation over the same stable
interval and compares their equivalent continuation state.

There are no legacy demo formats, suffix demos, standalone bisection repros,
tick-demo proof files, or frontend-timeline proof files in the 3.0 design.
Downstream recordings made with earlier formats must be recorded again.

## The verification model

```text
ReplayArtifact
  deterministic events + stable points + function visits
          │
          ├── oracle ExecutionProfile ── continuation cache ──┐
          │                                                   ├─ canonical projection ─ compare
          └── candidate ExecutionProfile ─ continuation cache ┘
```

An execution profile identifies one exact configuration:

- oracle or candidate role;
- interpreter, hooked/lifted, CPUless, DOS-memory-backed, or detached-native
  implementation;
- executable/lifted image;
- runtime and device model;
- continuation-state schema;
- canonical projection schema;
- installed override/function identities.

Changing any identity rejects that profile's cache. Other profiles recorded
under the same event stream remain usable.

## Stable points and events

A replay chooses one canonical monotonic timeline. `ReplayPoint` is
`(timeline_id, ordinal)`; the pair is its stable identity. Function entry,
function exit, frame, instruction count, crash, divergence, and manual names
are annotations on a point, not competing clocks.

`ReplayEvent` stores a point, stable sequence number, channel, and canonical
JSON payload. The event-stream hash is part of the artifact. Drivers apply the
same events according to their own representation while stopping at exactly
the requested point.

## Continuation state is not comparison state

This separation is the central 3.0 rule.

### ContinuationState

Private to one execution profile. It contains:

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

A fully native port is therefore an execution profile with a large faithful
override set. It uses the same replay corpus, points, function identities, and
verification operation as an early single-hook candidate.

## Persistent boundaries

Each profile owns one base continuation state. Every cached boundary stores:

- full continuation metadata and event cursor;
- only pages different from that profile's original base regions;
- page hashes and a complete reconstructed-state hash;
- the full execution-profile identity digest;
- the base continuation-state digest it was computed against.

Boundaries never form delta chains. Restoration always loads the profile base
and applies one boundary's changed pages. The closest cached point at or before
X is restored, replay advances to X, X is cached lazily, and only X→Y executes.

The on-disk shape is:

```text
demo/
  replay.json
  profiles/<profile-id-and-hash>/
    base/state.json
    base/regions/*.zlib
    boundaries/<point-key>/
      state.json
      pages/**/*.zlib
```

JSON and boundary publication use same-directory atomic replacement. Paths are
contained under the artifact root; compressed payload sizes and hashes are
verified before use.

## Validation and invalidation

Opening an artifact verifies its format and canonical event-stream hash.
Accessing a profile additionally verifies the executable/lifted image,
implementation, runtime, device model, continuation schema, projection schema,
and complete override-identity set. Restoring a boundary verifies the original
base-state digest, profile digest, point identity, region sizes, page hashes,
and reconstructed continuation digest. A mismatch raises `StaleReplayError` or
`ReplayError`; stale data is never silently restored or repaired in place.

Invalidation is deliberately coarse and safe. Changing the event stream means
recording a new artifact. Changing an execution profile creates a new
profile/cache namespace. Changing its base state invalidates every boundary in
that namespace. There is no partial cache migration.

## Differential verification

`verify_interval(artifact, oracle, candidate, X, Y)`:

1. validates artifact, event stream, timelines, and both profile identities;
2. restores each profile's nearest cache at or before X;
3. replays each profile exactly to X and compares their canonical state there;
4. rejects a non-equivalent X, otherwise lazily caches X for both profiles;
5. replays each side only from X to Y;
6. projects and compares both endpoints in their shared canonical schema;
7. caches Y for both profiles only when the endpoint is equivalent.

On mismatch, the already-diverged candidate endpoint is not cached as valid.
The artifact annotates X as the latest valid point before the observed
divergence.

## Bisection and repro

`bisect_divergence` receives stable candidate stop points. It verifies cached
sub-intervals until it finds the smallest supplied transition whose endpoint
diverges. The final valid predecessor is cached and annotated with the
divergent successor and profile identities.

That persistent point is the repro. There is no suffix demo or separate repro
snapshot format. After a fix, tooling restores the same pre-divergence point,
tests the small transition, then verifies the function's full first-entry to
last-exit interval.

## Function visits and atlas identity

`FunctionVisitIndex` records, per stable lifted/override function identity:

- total invocation count, including recursive calls;
- point immediately before the first entry;
- point immediately after the final completed outermost exit.

Nested and recursive depth is tracked. An invocation still active when replay
ends has no fabricated exit. These records become the execution atlas's
inverse index from function to covering replay artifacts.

## Runtime adapters

Each oracle or candidate supplies a `ReplayDriver`:

```python
class ReplayDriver:
    profile: ExecutionProfile
    current_point: ReplayPoint
    def capture() -> ContinuationState: ...
    def restore(state, point) -> None: ...
    def replay_to(artifact, point) -> None: ...
    def project() -> CanonicalState: ...
```

Game knowledge stays in adapters: event application, exact stop seams,
authoritative semantic fields, and stable lifted function identities. Storage,
validation, caching, interval selection, comparison, bisection, and repro
annotations stay in `dos_re.replay`.

## What was removed

dos_re 3.0 deliberately removes these independent workflows:

- game-tick demos and their private binary/digest format;
- frontend timeline proof containers;
- suffix demos that clone and rebase remaining input;
- timestamped divergence snapshot/repro manifests;
- repeated-prefix hook-set bisection.

Any useful proof concept from those systems must enter through
`CanonicalState` or stable replay points rather than retaining another artifact
format.

## Landing order

The implementation is intentionally separable:

1. Land artifact contracts, profile identities, stable points, and the
   base-relative boundary store.
2. Add real-mode and PM continuation codecs and prove capture/restore with
   deterministic resume tests.
3. Adapt hook-verification drivers to exact X→Y replay and replace repeated
   prefix bisection with persistent stable-point bisection.
4. Populate function visits during replay, then build the atlas inverse index
   from function identity to covering artifacts and intervals.
5. Add project-specific semantic projection schemas as candidates detach from
   DOS memory. Each projection lands independently and is checked against the
   same oracle corpus.

This slice implements steps 1 and 2 plus a driver-neutral end-to-end proof of
step 3. It does not prescribe atlas storage, native object models, or semantic
projection fields beyond the explicit interfaces above.
