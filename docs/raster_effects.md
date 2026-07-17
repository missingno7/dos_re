# Mid-frame palette changes as a recovered raster effect

## The problem class

A DOS game with indexed video and a small hardware palette can show more
simultaneous colors than the palette holds by CHANGING PALETTE STATE WHILE THE
FRAME IS BEING DISPLAYED.  Whatever the DAC holds when the beam draws a pixel
determines that pixel's color, so a palette rewrite mid-scan gives different
screen regions different effective palettes: copper-style splits, raster
blinks, fade bands, water-line color swaps.

A native renderer has no such limitation -- it produces a full-RGB frame -- but
a renderer that samples ONE palette per frame collapses the trick: every region
gets the same palette, and whichever region the sampled state belonged to wins.

Worked example (VGA Lemmings, machine type 2, measured from the port-effect
stream): every gameplay tick performs

    write DAC 16..23  <- CONTROL-BAR palette      (late in the display frame)
    read  3DAh until vertical-retrace bit rises   (the sync event)
    write DAC 0..7    <- level palette bank 0
    write DAC 16..23  <- LEVEL/terrain palette    (top of the next frame)

Attributes 8..15 map to DAC 16..23 in both screen regions; the game swaps the
bank mid-frame so the level area and the skill panel share attribute indices
but not colors.  A single-palette render paints the panel in terrain browns.

## Where the recovery lives (and why it is NOT a lifter pass)

The tempting design is static: teach the lifter to recognize palette-write +
raster-sync idioms and emit a semantic video-effect node.  That is the wrong
layer.  Idioms vary per game, recognition would be fragile, and -- decisive --
the information is already recovered: DOS_RE lifts port I/O as platform
effects with exact ordering and virtual timestamps, and the acceptance gates
prove the effect stream byte-exact across the interpreted oracle, the VMless
graph and the standalone CPUless program.  The lifting pipeline's contribution
to raster effects is already done the moment the corpus verifies.

The recovery therefore lives in the ONE place all three runtimes share: the
device model (``dos_re.dos.DOSMachine``).  Every 3DAh read and every DAC write
flows through it, in identical order, on every runtime -- so a journal there
is automatically consistent with whatever runtime is driving, with zero
game-specific and zero runtime-specific code.

## The journal (implemented, v1)

``DOSMachine`` splits DAC mutations into DISPLAY FRAMES at each OBSERVED
vertical-retrace edge -- a 3DAh read returning bit3=1 after one returning
bit3=0, i.e. the game's own synchronization event, which is meaningful under
the deterministic (non-time-source) status model precisely because the game's
perception is the ground truth there:

    _raster_open_dac    index -> (r,g,b): writes since the last observed edge
    raster_late_dac     the previous frame's accumulated writes (closed at
                        the edge)
    raster_split_palette()
                        the renderer contract: late-frame DAC values that
                        DIFFER from the live palette (empty on screens with
                        no mid-frame discipline)

The renderer composes: top band = live palette (the post-retrace state),
bottom band = live palette overlaid with ``raster_split_palette()``.  Screens
without the discipline degrade to the plain single-palette render, and the
journal is transient (rebuilds within one displayed frame after a snapshot
resume), so it is deliberately not snapshotted.

## What the journal cannot know: the split line

The game syncs to "the retrace edge", not to a counted scanline; where the
beam was when the late write landed is real-hardware timing that the
deterministic status model does not carry.  The evidence hierarchy:

1. **Observed scanline counting (v2, not yet needed):** a game that positions
   a split precisely waits for display-enable toggles (3DAh bit 0) and counts
   them -- each observed toggle pair is one perceived scanline.  The journal
   can record the count at each write and hand the renderer an exact row.
   (VGA Lemmings' front-end uses such loops for delays; its gameplay split
   does not count -- it relies on tick/refresh phase.)
2. **A display-locked time model (v3):** deriving beam position from virtual
   time requires the synthetic 3DAh status to follow a free-running display
   clock instead of toggling per read.  That changes observed port values,
   i.e. the whole demo corpus' meaning -- it is a deliberate future tier, not
   a patch.
3. **A declared port fact (v1, in use):** when neither source exists, the
   split row is game knowledge with evidence, exactly like a boundary head.
   Lemmings: the level viewport is 160 rows; the panel starts at y=160
   (``lemmings/render.py GAME_RASTER_SPLIT_Y``).

## Reuse in another port

Nothing to lift, nothing to configure in dos_re: if the game writes the DAC
mid-frame with any retrace discipline, ``raster_split_palette()`` is already
populated on every runtime.  The port's renderer decides where the split row
lives (fact or, once v2 exists, the counted scanline) and composes bands.
Attribute-controller (3C0h) journaling and multi-band timelines are the same
mechanism extended -- add them when a game demands them.
