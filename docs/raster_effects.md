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

Worked examples (VGA Lemmings, machine type 2, measured from the port-effect
stream):

* **Gameplay** (mode 0Dh): each tick writes DAC 16..23 with the CONTROL-BAR
  palette purely by code timing, syncs to the retrace, then reloads 0..7 +
  16..23 with the LEVEL palette.  Attributes 8..15 map to DAC 16..23 in both
  screen regions; the bank swap gives the level area and the skill panel the
  same attribute indices but different colors.
* **Level briefing** (mode 10h, routine at 1010:46B9): wait for the retrace
  edge pair, write the BRIGHT logo palette, count 60 scan lines by polling
  display-enable (``mov cx,3Ch`` / a test-loop pair per line), write the DARK
  text palette.  Rows 0..59 show the fiery logo, rows 60..349 dark rock and
  blue text.  A single-palette render shows the whole screen in whichever
  palette it sampled -- the historical bug this design replaced twice.

## The architecture: layered evidence, classification only when justified

    raw ordered hardware evidence          DOSMachine.video_journal
        -> normalized palette operations   palette_effects (cycles: waits +
                                           generations, virtual-time gaps)
        -> semantic classification         palette_effects.classify -- ONLY
           when justified                  when the evidence supports it;
                                           otherwise an explicit 'unresolved'
        -> native rendering                the port's rasterizer composes RGB
                                           bands / applies palettes natively

The raw journal survives even when the higher-level meaning is unresolved,
and nothing above the device layer consumes or mutates it.  The doctrine:
prefer a visible 'unresolved' over guessing visual intent from one game or
one screen.

### Why the recovery is device-side, not a lifter pass

Idioms vary per game and static recognition would be fragile -- and, decisive:
the information is already recovered.  DOS_RE lifts port I/O as platform
effects with exact ordering and virtual instruction counts, and the acceptance
gates prove the effect stream byte-exact across the interpreted oracle, the
VMless graph and the standalone CPUless program.  The one place all runtimes
share is the device model, so the journal lives there and is automatically
identical on every runtime -- including future memoryless generations, which
still route port effects through the device.

### Layer 1: the raw journal (``DOSMachine.video_journal``)

An append-only bounded deque of coalesced bus events:

    ("st",  reads, t_first, t_last)              maximal run of input-status
                                                 (3DAh/3BAh) reads; broken
                                                 only by a DAC event
    ("dac", start, (rgb, ...), t_first, t_last)  maximal run of completed DAC
                                                 triples at contiguous
                                                 ascending indices

Timestamps are virtual instruction counts -- deterministic machine state,
byte-exact across runtimes -- so the journal never enters acceptance digests
and never diverges.  It is transient presentation state (not snapshotted): it
rebuilds within one displayed frame after any resume, during which
classification reports 'static' and the renderer shows the live palette.

Crucially the journal does NOT depend on interpreting the synthetic retrace
bit's VALUE.  The previous design closed frames at each observed bit3 rising
edge; under the deterministic per-read-toggle status model an "edge" occurs
every second read, so a screen that polls heavily (the briefing's counting
loop reads 3DAh ~260x per tick) shredded the frame into ~130 fragments.  Run
LENGTHS and write ORDER are real evidence; fabricated bit transitions are not.

### Layer 2+3: normalization and classification (``dos_re.palette_effects``)

``classify`` slices the journal at frame syncs -- SHORT status runs
(<= SYNC_MAX_READS): an edge wait exits within a couple of reads under the
toggle model.  Within the most recent complete cycle:

* a LONG status run is a **counting delay**: the game counts display-enable
  cycles to reach a raster position.  The read count is the game-intended
  count; the device exports STATUS_READS_PER_SCANLINE (2 under the toggle
  model -- a test-loop pair per line) so ``line = reads / reads_per_line``.
  This is how the briefing's ~121-read run becomes the evidence-backed split
  line 60 with no game-specific code.
* a DAC group separated from the previous event by a large virtual-time gap
  (> LATE_GAP_STEPS) with no wait between is **code-timed**: it lands
  mid-frame but carries no position.  Its band line is None -- honestly
  unplaced.  (Gameplay's control-bar write: measured gap ~1100 instructions
  vs <= 54 within bursts.)

The result is a plain-data plan: ``{kind: static|split|unresolved, base,
bands: [{line, values}], evidence}``.  Temporal effects need no special
machinery: a blink shows up as successive cycles with different top palettes
and a fade as a per-cycle drift -- applying each cycle's plan renders both
faithfully (the briefing's fade-in composes with its spatial split for free).
Evidence the classifier cannot attribute -- more than one unplaced band, an
implausible counted line -- yields kind='unresolved', and the renderer is
expected to fall back to the frame-top palette and SAY SO (one ASCII notice),
never to guess.

### Layer 4: native rendering (the port)

The port's rasterizer translates the historical hardware technique into a
native operation: it overlays band palettes cumulatively in raster order and
recolors ``frame[row:]`` per band (``lemmings/render.py _decode_banded``).

> **Decode once, paint each row once.** Bands differ only in their palette, and
> the planar decode does not depend on the palette at all -- so decode the
> attribute INDICES once, then map each band's palette over its own row range.
> The obvious first cut (re-decode the whole frame per band, keep the rows
> below the split) does the expensive half twice and throws the first result
> away: measured 1.49 ms -> 0.83 ms per frame once fixed, on a screen with a
> single split. A faster renderer that changes a pixel is a broken one -- diff
> it against the old output frame by frame.
Row resolution is port knowledge: counted scan lines map 1:1 in mode 10h and
2:1 in double-scanned mode 0Dh; a band with line None is placed by a declared
port fact (Lemmings: the skill panel starts at y=160, GAME_RASTER_SPLIT_Y)
or dropped visibly when no fact exists.

## The split-line evidence hierarchy

1. **Counted scan lines (implemented):** the game's own display-enable
   counting, recovered from the run length.  Exact, zero configuration.
2. **A declared port fact (implemented):** for code-timed writes the port
   supplies the row with evidence, exactly like a boundary head.
3. **A display-locked time model (future fidelity tier):** deriving beam
   position from virtual time requires the synthetic 3DAh status to follow a
   real display clock.  That changes observed port values -- the meaning of
   the whole replay corpus -- so it is a deliberate future tier for oracle
   fidelity/diagnostics, NOT a prerequisite: native rendering does not need
   cycle-accurate CRT emulation when the visual meaning is recoverable and
   representable directly.

## Reuse in another port

Nothing to lift, nothing to configure in dos_re: any game whose palette
discipline flows through 3DAh waits and DAC writes journals and classifies
identically on every runtime.  A new port implements only the last layer --
mapping bands to rows (facts where evidence is silent) and composing RGB.
Attribute-controller (3C0h) journaling, hsync-granular effects and per-band
pel-panning are the same mechanism extended -- add journal event kinds when a
game demands them, never renderer special cases.
