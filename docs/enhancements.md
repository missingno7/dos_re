# The enhanced layer — faithful core vs presentation

The Prehistorik 2 port shipped modern comforts (widescreen, frame
interpolation, smooth transitions, stereo SFX, scaling) *without ever
diverging from the verified game*. That worked because of one architectural
rule, which every port built on this framework should adopt.

## The rule

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
(pitfall #18, and the bottom-up rule in [`lifecycle.md`](lifecycle.md)).

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
`tools/display.py`). But internal effects were often authored in raw square-
pixel coordinates. Both presentations are legitimate:

- **4:3 (par=1.2)** — historically authentic display shape.
- **Square pixels (par=1.0)** — preserves raw internal pixel geometry.

Make it a user-selectable presentation option. Neither affects gameplay or any
verification: frame verification compares the framebuffer *before*
presentation scaling.

## Status labeling

Mark enhanced-layer code `PRESENTATION_ONLY` in its module docstring, and keep
it out of the recovered/ layers entirely. Anything that would write game state
is not an enhancement — it is either a recovered feature (goes through the
oracle) or it doesn't ship.
