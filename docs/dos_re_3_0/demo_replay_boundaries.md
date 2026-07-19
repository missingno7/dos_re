# Demo replay artifacts and persistent boundaries

Status: phase-1 design and narrow proof slice. This document does not declare
the real-mode or protected-mode snapshot formats to be complete continuation
state yet.

## Purpose

For this workstream, a **demo** means only a deterministic recording used to
run the same interval against the original interpreter and a hooked or lifted
candidate. Its purpose is oracle-versus-candidate differential verification.
Such a demo becomes a navigable execution artifact, not only an input
recording. It owns:

- one original base snapshot;
- one deterministic event/input stream;
- one stable, ordered timeline;
- descriptive and identity metadata;
- a lightweight function-visit index;
- an optional persistent set of independently restorable replay boundaries.

The first consumer is interval replay. Given points `X <= Y`, the replay
system restores the nearest valid cached boundary at or before X, replays to
X, caches X if needed, and then executes only X through Y.

This is deliberately narrower than a universal execution-atlas design. The
format gives later work stable identities and extension points without
choosing a global function model, SMC policy, provenance schema, or query
language now.

## Scope

In scope:

- the deterministic input-demo bundles already used to drive interpreted
  oracle and hooked/lifted candidate runtimes;
- exact replay of a selected verification interval on both sides;
- complete endpoint comparison sufficient to prove that unchanged execution
  after the interval is redundant;
- persistent entry, exit, and latest-valid pre-divergence boundaries that make
  repeated hook verification cheaper;
- function coverage metadata used to select the relevant demo and interval.

Out of scope:

- tick demos intended to bridge VM and VM-less gameplay;
- frontend timelines, viewer recordings, playback-only captures, and other
  demo-like artifacts;
- snapshot indexing that does not directly accelerate interpreter-versus-hook
  differential replay.

An out-of-scope artifact does not become part of this workstream merely because
it has events, snapshots, frames, or a deterministic clock.

## Survey of the current paths

### Demos and clocks

- `dos_re.input_demo` stores real-mode input events in `input_demo.json`.
  Events use an adapter/front-end boundary counter. A bundle optionally owns a
  `snapshot/`; cold-start demos have no snapshot. Playback maintains an
  in-memory event cursor, and suffix creation writes a new full snapshot plus
  rebased remaining events.
- `dos_re.pm_input_demo` stores protected-mode events keyed to an
  adapter-supplied frame hook. Its bundle also optionally owns a PM snapshot.
  Per-frame digests cover memory and CPU state, but not every device or host
  continuation field.

The phase-1 replay index uses one canonical monotonic ordinal chosen by the
hook-verification replay driver. Frame, instruction count, function entry,
divergence, crash, and manual names are annotations or aliases to an ordinal;
they are not competing ordering systems.

### Snapshots

- `dos_re.snapshot` writes real-mode `state.json` plus `memory_1mb.bin`.
  It captures CPU registers, instruction count, DOS bookkeeping, several
  video/PIT/OPL fields, console input, open-file positions, and optional Sound
  Blaster state. Restore reconstructs a runtime shell and applies these fields.
- `dos_re.pm_snapshot` has a stronger shared capture/apply seam:
  `capture_pm_state` and `apply_pm_state` cover CPU, selector/x87 state,
  DOS/4GW host state, PIC, VGA, optional Sound Blaster, flat memory, and VGA
  planes. Save/load and verifier cloning consume the same representation.
- `dos_re.repro_artifacts` and verifier exceptions preserve full pre-operation
  snapshots, but those outputs are isolated repros rather than reusable demo
  boundaries.

The existing snapshot families are foundations, not yet a single
`complete deterministic continuation state` contract. In particular, endpoint
equivalence must not be inferred from an existing memory/CPU digest. Each
runtime codec must enumerate and test all device, timer, interrupt, scheduler,
open-resource, event-cursor, and host/runtime state that can affect later
execution.

### Differential and bisection

- `dos_re.verification` and `dos_re.pm_verification` clone pre-hook state, run
  the original oracle and candidate, and compare their results.
- `dos_re.frame_verify` demonstrates paired reference/candidate replay and can
  preserve a pre-frame repro. Only its use with hooked/lifted candidates is
  relevant here.
- `tools/hook_bisect.py` repeatedly starts from the same snapshot, scans for a
  first divergent boundary, and bisects installed hooks. A driver may write a
  suffix repro, but the bisect outputs are not indexed under the source demo.

Persistent replay boundaries replace repeated prefix execution inside this
workflow; they do not replace the original oracle or its differential
comparisons.

## Terminology and identities

### Demo point

A `DemoPoint` is `(timeline_id, ordinal)`.

- `timeline_id` identifies the canonical timeline definition, for example
  `frame-hook-v1:00119d40` or an adapter-defined instruction-boundary clock.
- `ordinal` is a non-negative integer and totally orders points on that
  timeline.
- The stable point key is derived only from those two fields.

Labels and observations are separate metadata attached to a point. A single
point may be named `frame 120`, `first-entry func:1234`, and
`latest-valid-before-divergence` without acquiring three identities.

The driver must stop exactly at a point before reporting it. “Immediately
before an operation” and “immediately after a completed return” are distinct
ordinals chosen by the timeline producer; the storage layer does not guess
instruction phases.

### Function identity

The phase-1 index treats a lifted function identity as an opaque stable string
owned by the lifter/image manifest. Address text may be part of that string,
but consumers must not parse it. A future versioned-code identity can replace
or qualify it without changing replay-boundary identity.

For each observed function the demo records:

- total invocation count, including recursive invocations;
- the point immediately before its first outermost entry;
- the point immediately after its last completed outermost exit.

The recorder tracks per-function active depth. Recursive entries increment the
invocation count but do not move the outer interval start; only the return that
brings depth back to zero updates `last_exit`. An invocation still active when
recording stops has no completed exit and must not synthesize one.

### Cache identity

Every base and cached boundary is tied to:

- canonical event-stream hash;
- base-snapshot hash;
- executable or lifted-image identity/hash;
- runtime implementation identity/hash;
- device-model identity/hash;
- snapshot/continuation-state format identity.

These fields form one cache-identity digest. Opening an index requires the
expected identity. A mismatch rejects the cache before restoration. Cache
entries also repeat the digest so a copied or partially replaced boundary
cannot be accepted under a different index.

Runtime and device identities are explicit inputs in phase 1. Later build
tooling may compute them from package manifests or source trees, but a vague
package version is not silently substituted for a content identity.

## Artifact layout

The narrow implementation stores an optional `replay/` directory inside an
existing demo bundle:

```text
demo_name/
  input_demo.json                 existing event stream and base-snapshot link
  snapshot/                       existing original base snapshot
  replay/
    index.json                    format, identity, points, function visits
    base/
      state.json                  non-memory continuation metadata + cursor
      regions/<name>.bin.zlib     full base memory regions
    boundaries/<point-key>/
      state.json                  point, identity, metadata, changed-page list
      pages/<region>/<n>.bin.zlib changed pages only
```

The phase-1 base copy makes the generic slice self-contained and testable. A
runtime-specific migration may later make `base/` reference the demo's
canonical snapshot files, provided the base hash covers exactly the bytes and
metadata restored.

`input_demo.json.metadata.replay_index` points to `replay/index.json`.
Function visits are stored in the replay index and mirrored into the demo
metadata so existing catalog tools can discover them without loading cached
pages.

## Snapshot-delta semantics

The runtime codec captures a `MachineImage`:

- JSON-serializable non-memory continuation state;
- an explicit demo-event cursor;
- one or more named byte-addressable memory regions.

The base stores all regions. A cached boundary stores the complete non-memory
state and cursor plus only pages whose bytes differ from the same page in the
original base. The final partial page is valid. Region names and lengths must
match the base exactly.

Restoration is always:

1. load the original base metadata and full regions;
2. copy the base regions;
3. apply the selected boundary's changed pages;
4. replace the base non-memory state and cursor with the boundary values;
5. give the reconstructed complete image to the runtime codec.

No boundary references another boundary. Deleting any cached boundary cannot
make another unrestorable.

Each compressed page has a byte length and SHA-256 in its manifest. Restore
checks path containment, page index, expected page length, decompression, and
hash before applying it.

## API boundary

The storage/replay layer is machine-neutral so the real-mode and protected-mode
hook verifiers can share it:

```python
class ReplayDriver:
    @property
    def current_point(self) -> DemoPoint: ...
    def capture(self) -> MachineImage: ...
    def restore(self, image: MachineImage, point: DemoPoint) -> None: ...
    def replay_to(self, point: DemoPoint) -> None: ...
```

`replay_interval(artifact, driver, x, y, cache_y=False)`:

1. rejects different timelines, `Y < X`, or stale cache identity;
2. chooses the greatest cached point `<= X` (the base is always a candidate);
3. restores it and calls `replay_to(X)`;
4. captures and persists X if absent;
5. calls `replay_to(Y)`;
6. captures Y for the result, and persists it when requested;
7. lets the existing differential verifier compare that complete endpoint
   against the other side's endpoint for the same X→Y interval.

The driver owns deterministic input injection and exact stopping. Its captured
event cursor is part of `MachineImage`, not replay-layer bookkeeping.

An endpoint verifier receives the complete captured `MachineImage` (or a
runtime-specific canonical continuation digest derived from it). A RAM-only or
register-only comparator is not accepted as proof that the remaining demo is
redundant.

## Cache validation, invalidation, and writes

- Identity mismatch is a hard `StaleReplayCacheError`; stale data is never
  silently restored or automatically relabeled.
- Structural corruption, missing pages, wrong hashes, wrong region sizes, and
  an impossible current point fail loudly.
- Index and JSON manifests are written via same-directory temporary files and
  atomic replacement. A boundary directory is published only after its files
  are complete.
- A cache rebuild creates a new replay index from the base. Automatic deletion
  policy is intentionally deferred; rejection is safer than surprising data
  loss.
- Concurrent writers are not supported in phase 1. A later implementation
  should add a bundle-local lock before atlas/query services write in parallel.

## Divergence boundaries

The verifier must preserve the latest point known to match immediately before
the first mismatching operation. It attaches mismatch metadata to that valid
point:

- compared oracle/candidate identities;
- operation or transition about to execute;
- first observed mismatch kind and location;
- the divergent successor point, if known.

It must not cache the already-diverged candidate state as the replay start.
Phase 1 provides point annotations and persistent boundaries; wiring the hook
verifiers and their paired replay drivers to publish these records is a later
phase.

## Compatibility and migration

- Existing real-mode input demo versions 1–2 and PM demo versions 1–2 remain
  readable and unchanged. `replay/` and its manifest metadata link are optional.
- Cold-start demos can be indexed after a deterministic run captures their
  ordinal-zero state. They are not rewritten into snapshot-anchored demos
  implicitly.
- Existing suffix demos and bisection repros remain valid. A future importer
  may promote a verified suffix start into a boundary, but must prove it uses
  the same base, event stream, timeline, and complete-state codec.
- Tick demos, frontend timelines, and other playback artifacts are not
  migration targets for this workstream.

## Dependency order and phased implementation

1. **Generic format proof (this slice).** Stable ordered points, cache
   identities, complete opaque state + named memory regions, base-relative
   changed-page storage, nearest-boundary restore, lazy X caching, exact X→Y
   replay, and a recursive-safe function-visit index.
2. **Continuation-state audits.** Define and test real-mode and PM codecs.
   Resume from arbitrary points and compare uninterrupted continuation,
   including devices, timers, pending interrupts, files, scheduler/runtime
   state, and event cursor.
3. **Hook-demo integration.** Have the deterministic recorders used by hook
   verification create identity metadata and timelines; teach the paired
   oracle/candidate replay drivers exact point stopping and cursor restoration.
4. **Function instrumentation.** Feed stable lifted identities and call/return
   observations into the visit recorder; publish inverse demo coverage later
   through the execution atlas.
5. **Hook-verifier integration.** Select function intervals, replay the same
   X→Y interval against interpreter and hooked/lifted sides, compare complete
   endpoint state, and persist latest-valid pre-divergence boundaries.
6. **Migration and tooling.** Import eligible suffix/bisect artifacts, add
   cache inspection/rebuild commands, locking, size policy, and garbage
   collection.

The execution atlas, memory-access provenance, versioned code/SMC model, and AI
query layer depend on the stable point/function/cache identities above. They
do not need to be designed to complete phases 1–3.
