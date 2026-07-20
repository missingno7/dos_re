> Current policy reference for `OverrideCategory.ENHANCEMENT`. Execution and
> release closure remain owned by [`execution_planner.md`](execution_planner.md).

# Non-authoritative enhancements over verified game state

> This file defines the read-only authoritative-state boundary and verification
> policy for presentation and host integrations.
> Enhancement registration and verification policy are defined by
> [`override_architecture.md`](override_architecture.md).

The Prehistorik 2 port shipped modern comforts (widescreen, frame
interpolation, smooth transitions, stereo SFX, scaling) *without ever
diverging from the verified game*. That worked because of two rules — one
about the boundary, one about the **order** — and every port built on this
framework should adopt both.

## The sequencing rule: verify the authoritative seam first

**An enhancement may attach only after the authoritative state it consumes has
a verified read-only seam.** It does not require the entire game to be
memoryless. The unchanged surrounding program may still run through the
interpreter, generated VMless code, or generated CPUless code:

```text
baseline backend → verified authoritative state seam → read-only enhancement
                 \____________________________________/ replay state comparison
```

Do not use an enhancement to invent missing gameplay state or hide an
unverified subsystem. If presentation needs information the baseline does not
expose, recover and verify that state seam first. This permits useful
renderers, audio outputs, host UI, and debug visualization to land
incrementally without turning presentation into a second authority.

## The boundary rule

**Enhancements are pure presentation: they read game state and write none.**

- The **faithful core** owns gameplay, timing, collisions, RNG, object state,
  level state, input semantics — everything the oracle verifies. It is
  byte-comparable against the original forever.
- The **enhanced layer** owns widescreen, interpolation, scaling, CRT vs
  square-pixel aspect, stereo expansion, modern UI/options, fullscreen. It may
  intentionally diverge from the original's *frame output*; it must never
  mutate gameplay state.

Enforce it, don't aspire to it: pre2 proved every enhancement pixel-/state-
equal to the faithful game at its neutral setting (the "alpha=1 parity gate"),
so *enhanced never means diverged* — it is the same game, shown better. An
enhancement that needs data the faithful core doesn't expose gets that data
**recovered at the source layer first** — never faked in the renderer
(pitfall #18). The same boundary applies whether authoritative state is still
DOS-memory-backed or has moved into detached objects.

The seam enhancements attach to is a **semantic render-intent model** emitted
by the faithful renderer (sprites, camera, palette, transition state) —
*derived from* the canonical state capture, never a second parallel truth.
Frame interpolation then needs only a rolling two-snapshot window (pre2's
`frame_capture.py` pattern) and lerps presentation, not simulation.

## The widescreen lesson (why "just render wider" is wrong)

True widescreen is not drawing a wider background. Before widening anything,
answer from the oracle:

- Are objects/projectiles/particles **culled at the 320-px window** by the
  original code? Drawing the margins then shows pop-in — or nothing.
- Does the original **producer/spawner** only create entities near the
  window? *Advancing the producer to fill the margins changes gameplay* —
  that is a simulation mutation wearing a presentation costume. Forbidden.
- Are foreground overlays and HUD chrome still clipped correctly?
- Some content genuinely can't widen (pre2's gorilla-boss levels draw from
  off-screen tiles); the honest answer there is 4:3 content with a wide HUD.

So widescreen decomposes into: safely-widenable layers (real extra tilemap
columns), presentation-only choices (HUD placement, edge treatment), and
untouchable simulation (producers, culling that feeds back into state). Pre2's
"true widescreen" mode draws already-simulated objects out into the margins —
it never simulates more of the world.

## The pixel-aspect lesson

320×200 DOS games were displayed on 4:3 CRTs — pixels 1.2× tall (`par=1.2` in
`dos_re.display`). But internal effects were often authored in raw square-
pixel coordinates. Both presentations are legitimate:

- **4:3 (par=1.2)** — historically authentic display shape.
- **Square pixels (par=1.0)** — preserves raw internal pixel geometry.

Make it a user-selectable presentation option. Neither affects gameplay or any
verification: frame verification compares the framebuffer *before*
presentation scaling.

## Status labeling

Declare enhanced code as a non-authoritative enhancement in the unified
override registry and keep it out of generated baseline directories. Anything
that writes authoritative game state is not an enhancement: it is either a
faithful replacement or an explicitly declared behavioral modification.
