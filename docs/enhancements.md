# The enhanced layer — faithful core first, presentation last

The Prehistorik 2 port shipped modern comforts (widescreen, frame
interpolation, smooth transitions, stereo SFX, scaling) *without ever
diverging from the verified game*. That worked because of two rules — one
about the boundary, one about the **order** — and every port built on this
framework should adopt both.

## The sequencing rule: the enhanced layer is the endgame

**Recover a complete, working, faithful native game first. Build enhanced
rendering/presentation on top of it, last.** The lifecycle is:

```text
ASM game running in VM → deterministic replay / snapshots → hooks and routine
replacement → verified recovered islands → faithful native subsystems →
COMPLETE faithful VM-less game → enhanced presentation layer
```

Do not start widescreen, interpolation, smooth transitions, or an "enhanced
renderer" while the faithful core still has unrecovered subsystems. Early in
its history, the Prehistorik 2 project experimented with growing a
faithful/enhanced visual backend *alongside* recovery (the "cyborgization"
phase — see `pre2_port/docs/pre2/faithful_visual_layer.md` for the machinery
it required). It ultimately worked, but the project's own verdict is **not
recommended**: it created transitional not-yet-grounded states to police,
parallel-truth risks to audit, and presentation effort spent while gameplay
was still unrecovered. The enhanced engine is the *reward* for a finished
faithful game, not a recovery shortcut.

**The one sanctioned exception class: audio-style disruptions.** P2 improved
audio playback earlier than the rule allows because it was (a) relatively
simple and separable, and (b) the original emulated playback was noisy and
crackling enough to disrupt all other work. That is the bar for an exception:
small, cleanly separable, and fixing something that actively impedes the
recovery workflow itself — and it still obeys the boundary rule below. "It
would look nicer" never qualifies.

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
(pitfall #18). Note that with the sequencing rule above this situation should
be rare: by the time the enhanced layer is being built, the faithful core is
complete.

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
