"""Normalization + semantic classification of raw video-journal evidence.

Layer 2 and 3 of the raster-effects architecture (docs/raster_effects.md):

    raw ordered hardware evidence   (DOSMachine.video_journal -- device side)
      -> normalized palette operations   (cycles of generations + waits)
      -> semantic classification WHEN JUSTIFIED (this module)
      -> native rendering                (the port's rasterizer)

The input is the device's ordered, coalesced event stream:

    ("st",  reads, t_first, t_last)               status-register read run
    ("dac", start, (rgb, ...), t_first, t_last)   DAC write group

and the output is a plain-data *palette plan* for the most recent complete
display cycle:

    {"kind": "static" | "split" | "unresolved",
     "base":  {index: (r, g, b)},          # palette state at the frame top
     "bands": [{"line": int | None,        # scan line the band starts at;
                                           # None = code-timed write, the
                                           # evidence cannot place it (a port
                                           # fact must, or it stays unshown)
                "values": {index: (r, g, b)}}],
     "evidence": "ascii summary"}

Doctrine: classify only what the evidence supports; prefer an explicit
'unresolved' over guessing visual intent.  The raw journal stays available
either way (nothing here consumes or mutates it).

What the evidence can and cannot say
------------------------------------
* A SHORT status run (<= SYNC_MAX_READS) is an edge wait -- the game's own
  frame synchronization.  Cycles are delimited by these.
* A LONG status run is a counting delay: the game counts display-enable
  (scan-line) cycles to reach a raster position.  Its read count IS the
  intended count -- under the deterministic per-read-toggle status model one
  scan line costs `reads_per_line` reads (the device exports the constant, so
  a future display-locked model plugs in without touching this module).
  VGA Lemmings' briefing screen counts 60 lines (cx=3Ch at 1010:46D4) and
  journals as a ~120-read run.
* A DAC group separated from the previous event by a LARGE virtual-time gap
  (> LATE_GAP_STEPS instructions) with no status run between is CODE-TIMED:
  it lands somewhere mid-frame but the port level carries no position.  Its
  band line is None -- honestly unresolved; the port supplies the row as an
  evidence-backed fact or the renderer visibly skips it.  (VGA Lemmings'
  gameplay control-bar palette is exactly this; split row 160 is the fact.)
* Temporal effects need no bands: palette alternation (blink) shows up as
  successive cycles carrying different top palettes, and a fade as a slow
  drift -- both render correctly by simply applying each cycle's palette, so
  they classify as consecutive 'static'/'split' plans, never as a guess.

Thresholds are normalization facts, not tuning knobs: an edge wait exits
within a couple of reads under the toggle model, a counting delay takes
dozens; palette-writer bursts are tens of instructions apart (measured <= 54
across every VGA Lemmings screen), code-timed writes land >= ~1100
instructions out.  Each threshold sits > 4x away from both populations.
"""
from __future__ import annotations

#: A status run at most this many reads long is an edge wait (frame sync).
SYNC_MAX_READS = 6
#: A DAC group at least this many virtual instructions after the previous
#: journal event (with no wait between) is code-timed ("late") rather than
#: part of the same burst.
LATE_GAP_STEPS = 256
#: A counted band line beyond any real vertical resolution means the
#: evidence is corrupt -- classify unresolved rather than render nonsense.
MAX_PLAUSIBLE_LINE = 1024


def _plan(kind, base=None, bands=None, evidence=""):
    return {"kind": kind, "base": dict(base or {}),
            "bands": list(bands or []), "evidence": evidence}


def classify(events, reads_per_line: int = 2) -> dict:
    """Classify the most recent complete display cycle in `events`.

    `events` is the device journal as an ordered sequence (oldest first).
    Returns a palette plan (see module docstring).  With fewer than two
    observed frame syncs, or no palette writes between them, the screen has
    no per-frame palette discipline: 'static'.
    """
    events = tuple(events)
    sync_at = [i for i, e in enumerate(events)
               if e[0] == "st" and e[1] <= SYNC_MAX_READS]
    if len(sync_at) < 2:
        return _plan("static", evidence="syncs=%d" % len(sync_at))
    s0, s1 = sync_at[-2], sync_at[-1]
    cycle = events[s0 + 1:s1]
    if not any(e[0] == "dac" for e in cycle):
        return _plan("static", evidence="empty cycle")

    bands = [{"line": 0, "values": {}}]
    line = 0
    last_t = events[s0][3]          # end of the opening sync run
    notes = []
    for e in cycle:
        if e[0] == "st":
            # A counting delay positions everything after it.  (A short
            # mid-cycle sync would be a second frame boundary; the slicing
            # above guarantees none occurs here.)
            line += int(round(e[1] / float(reads_per_line)))
            notes.append("count:%d->line %d" % (e[1], line))
            if bands[-1]["values"]:
                bands.append({"line": line, "values": {}})
            else:
                bands[-1]["line"] = line
            last_t = e[3]
        else:
            _, start, vals, t0, t1 = e
            if t0 - last_t > LATE_GAP_STEPS and bands[-1]["values"]:
                notes.append("late gap:%d" % (t0 - last_t))
                bands.append({"line": None, "values": {}})
            for i, rgb in enumerate(vals):
                bands[-1]["values"][(start + i) & 0xFF] = tuple(rgb)
            last_t = t1
    bands = [b for b in bands if b["values"]]
    evidence = "; ".join(notes) or "single burst"
    if not bands:
        return _plan("static", evidence=evidence)

    base = bands[0]["values"] if bands[0]["line"] == 0 else {}
    rest = bands[1:] if base else bands
    if not rest:
        return _plan("static", base, evidence=evidence)
    unresolved = sum(1 for b in rest if b["line"] is None)
    implausible = any(b["line"] is not None and
                      not (0 < b["line"] <= MAX_PLAUSIBLE_LINE) for b in rest)
    if unresolved > 1 or implausible:
        return _plan("unresolved", base, rest, evidence)
    return _plan("split", base, rest, evidence)
