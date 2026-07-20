# Replay evolution before dos_re 3.0

> Historical design record only. Nothing in this document is a supported API,
> file format, command, or implementation recipe. For current replay behavior,
> use [`../demos_and_snapshots.md`](../demos_and_snapshots.md).

Before dos_re 3.0, replay and verification evolved as several independent
experiments. Input recordings, game-tick verification, front-end flow
verification, divergence reproductions, and hook-set localization each
introduced their own persistence or control model. Those experiments exposed
requirements that the current architecture preserves without preserving their
formats or APIs.

## Lessons retained

- A replay clock must describe game progress, not incidental interpreter work.
  A hook can replace many instructions with one host call, and a detached
  implementation may have no meaningful instruction count at all.
- Input must be captured where the program consumes it. Sampling too early can
  miss an interrupt or polling transition that changes the consumed value.
- State without a shared raw layout needs an explicit semantic projection.
  Native object layout is not evidence of equivalence with DOS memory.
- Front-end and gameplay flows can use different stable seams while still
  belonging to one ordered replay timeline.
- A useful divergence reproduction is the latest equivalent state immediately
  before the failing transition, not an already-diverged snapshot.
- Replaying a full prefix for every experiment wastes most verification time.
  Persistent independently restorable boundaries make exact interval replay
  possible.
- Cache validity depends on the event stream, base state, executable image,
  runtime, devices, continuation schema, and installed implementation.

## Why the earlier mechanisms were retired

The earlier designs created parallel authorities: separate event manifests,
snapshot bundles, suffix recordings, tick-oriented proof files, front-end
timeline containers, timestamped reproduction bundles, and repeated-prefix
hook-set searches. They could disagree about event position, state ownership,
cache freshness, or what constituted a successful endpoint.

dos_re 3.0 replaces those authorities with one `ReplayArtifact` contract:
immutable events and stable points, backend-specific complete continuation
state, backend-neutral canonical comparison state, profile-local base-relative
boundaries, persistent divergence annotations, and function-visit metadata.

No compatibility loader or migration path is retained. Old recordings are
discarded and recorded again through the current players.
