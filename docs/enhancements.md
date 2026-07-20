# Non-authoritative enhancements

A non-authoritative enhancement changes presentation or host integration
without changing authoritative program behavior. It is declared with
`OverrideCategory.ENHANCEMENT` in the implementation catalog and selected
explicitly by an execution configuration. Catalog selection does not make an
implementation read-only automatically: read-only behavior is a declared and
tested policy contract enforced by the port's adapter, projections, and
verification.

See [`override_architecture.md`](override_architecture.md) for catalog
ownership and [`execution_planner.md`](execution_planner.md) for composition
and release policy.

## Contract

An enhancement declaration identifies:

- the authoritative state projection it consumes;
- the presentation or host output it owns;
- any host services and assets it requires;
- the output domains intentionally excluded from parity comparison;
- tests showing that enabling it does not mutate authoritative state.

The adapter should expose only the state needed by the enhancement and separate
writable presentation sinks from authoritative storage. Python cannot make an
arbitrary object graph transitively immutable, so a wrapper or convention is
not proof. Verification is the enforcement boundary.

An implementation that writes gameplay, timing, collision, RNG, object, level,
input, scheduler, or other authoritative state is not an enhancement. It must
be classified as either a faithful replacement or a behavioral modification.

## Attachment seam

An enhancement may attach as soon as its input seam is understood and verified;
the rest of the program may still use any mixture of interpreted, generated,
ABI-recovered, DOS-memory-backed, or native implementations. It does not require
global memoryless or EXE-detached execution.

The seam may be a read-only DOS-memory view, a canonical semantic projection,
or a recovered presentation-intent model. It remains a projection of the
authoritative state, not a second source of gameplay truth. If required data is
missing, recover and verify that state at its owning layer instead of deriving
plausible gameplay facts in presentation code.

## Verification

For the same replay identity and interval:

1. run the selected authoritative composition with the enhancement disabled;
2. run it with the enhancement enabled;
3. compare complete authoritative continuation state or the declared canonical
   semantic projection;
4. exclude only the enhancement's declared presentation outputs;
5. test enable/disable transitions and neutral/default settings when supported.

An intended framebuffer, audio, window, controller, or host-UI difference is
acceptable only inside the declared output domain. Any authoritative
difference remains a failure. A visual parity check alone cannot prove the
enhancement contract.

## Common design boundaries

- Widescreen presentation may reveal already-authoritative world state; it
  must not advance producers, extend collision ranges, or simulate additional
  entities merely to fill the new area.
- Interpolation blends presentation samples and must not advance simulation or
  write interpolated values back into authoritative state.
- Display scaling and pixel-aspect correction operate after the authoritative
  framebuffer or render intent used by verification.
- Modern audio output may transform a declared audio stream, but must not alter
  authoritative mixer, timing, or interrupt state.
- Gamepad or host input integration normalizes into the same deterministic
  input semantics used by replay.

Historical port-specific lessons are preserved separately in
[`history/enhancements_2.0.md`](history/enhancements_2.0.md).
