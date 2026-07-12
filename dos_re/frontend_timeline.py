"""Front-end (non-gameplay) TIMELINE capture + compare — prove a VM-less native front end reproduces the VM.

The tick-demo harness (:mod:`dos_re.tick_demo`) proves the GAMEPLAY core byte-for-byte, but it is keyed to game
TICKS and captures ZERO of the front end (intro / title / menu / attract / map / the score & tally screens run with
no gameplay tick). Those non-gameplay screens are exactly where a VM-less native port drifts undetected — the flow
is host-loop code verified against nothing (a wrong screen ORDER, a dropped fade, a screen shown before/after the
wrong transition). This module is the front-end analogue of the tick demo: a per-PRESENT-FRAME timeline.

The idea (mirrors overkill_port/scripts/probe_coldstart_frontend.py, generalized): at every present-frame boundary
record a compact witness of what is on screen — a COARSE logical SCREEN id (which screen, e.g. "title" / "menu" /
"the wall") plus a pixel digest (sha1 of the rendered RGB). Capture that timeline from the reference VM (ground
truth) and from the VM-less native front end, then diff:

  * SEQUENCE (always): the ordered run-length list of distinct screens (+ each run's frame count). Catches the
    common front-end bugs — a screen shown out of order, an extra/missing screen, a wildly wrong duration — and is
    robust to sub-frame pacing noise (a fade one frame longer is a small duration delta, not a screen mismatch).
  * PIXELS (opt-in): the per-frame RGB digest, frame-for-frame. The strongest proof (byte-exact rendering + exact
    cadence), but requires the two sides' frame cadences to align, so it is opt-in on top of the sequence gate.

Game-agnostic: the caller supplies the frame-advance + per-frame sampling (a ``sample(i) -> (screen, rgb_sha)``);
this module owns the timeline structure, the run-length collapse and the two comparisons. A port adds a thin
adapter (VM: replay a demo / boot to the front-end entry and render the framebuffer; native: drive the front-end
scene generator and render each scene) — see pre2_port/scripts/probe_frontend_timeline.py + verify_native_frontend.py.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional


def rgb_sha(rgb) -> str:
    """The canonical per-frame pixel digest: sha1 of the C-contiguous RGB array (or "" for a blank/None frame).

    Both sides MUST digest the same way for the pixel diff to mean anything — always go through this helper.
    Stdlib-only: ``tobytes()`` yields C-order bytes (like ``np.ascontiguousarray``) for a numpy array; bytes-like
    inputs are hashed directly. dos_re core stays stdlib-only (no numpy import)."""
    if rgb is None:
        return ""
    buf = rgb.tobytes() if hasattr(rgb, "tobytes") else bytes(rgb)
    return hashlib.sha1(buf).hexdigest()


@dataclass(frozen=True)
class FrameRecord:
    """One present-frame of a front-end timeline."""
    frame: int
    screen: str        # coarse logical screen id, e.g. "13h:MENU.SQZ" / "0Dh:map" / "oldies"
    rgb_sha: str       # sha1 of the rendered RGB frame ("" when the display is blanked / no frame)


@dataclass(frozen=True)
class ScreenRun:
    """A maximal run of consecutive frames on the SAME logical screen."""
    screen: str
    start: int         # first frame index of the run
    count: int         # number of frames in the run

    def __repr__(self) -> str:
        return f"{self.screen}x{self.count}@{self.start}"


def capture(sample: Callable[[int], Optional[tuple]], max_frames: int) -> "list[FrameRecord]":
    """Drive a front end one present-frame at a time and record its timeline.

    ``sample(i)`` advances ONE present-frame and returns ``(screen: str, rgb_sha: str)`` for it, or ``None`` when
    the front end ended (a level loaded / the generator stopped). Stops at ``max_frames``. The caller owns *how* a
    frame advances (VM step budget / native scene generator) and *how* the screen is classified + digested."""
    records: "list[FrameRecord]" = []
    for i in range(max_frames):
        out = sample(i)
        if out is None:
            break
        screen, sha = out
        records.append(FrameRecord(i, screen, sha))
    return records


def collapse(records: "list[FrameRecord]") -> "list[ScreenRun]":
    """Run-length compress a timeline into the ordered list of distinct-screen runs (the SEQUENCE)."""
    runs: "list[ScreenRun]" = []
    for r in records:
        if runs and runs[-1].screen == r.screen:
            runs[-1] = ScreenRun(runs[-1].screen, runs[-1].start, runs[-1].count + 1)
        else:
            runs.append(ScreenRun(r.screen, r.frame, 1))
    return runs


@dataclass
class SequenceDiff:
    """The result of comparing two screen SEQUENCES."""
    ok: bool
    index: Optional[int] = None       # first run index that differs (screen mismatch or over-tolerance duration)
    reason: str = ""
    a: Optional[ScreenRun] = None     # the reference run at ``index`` (None if the reference ran out of runs)
    b: Optional[ScreenRun] = None     # the candidate run at ``index``


def diff_sequence(ref: "list[ScreenRun]", cand: "list[ScreenRun]", *,
                  duration_tolerance: Optional[int] = None) -> SequenceDiff:
    """Compare two screen sequences run-for-run. Screens must match in ORDER; each run's frame COUNT must match
    within ``duration_tolerance`` (``None`` = ignore durations, compare only the screen order). Returns the first
    divergence — a screen mismatch, an over-tolerance duration delta, or one side having more/fewer runs."""
    n = max(len(ref), len(cand))
    for i in range(n):
        a = ref[i] if i < len(ref) else None
        b = cand[i] if i < len(cand) else None
        if a is None or b is None:
            return SequenceDiff(False, i, "extra screen" if b is not None else "missing screen", a, b)
        if a.screen != b.screen:
            return SequenceDiff(False, i, f"screen {a.screen!r} != {b.screen!r}", a, b)
        if duration_tolerance is not None and abs(a.count - b.count) > duration_tolerance:
            return SequenceDiff(False, i, f"{a.screen}: {a.count} vs {b.count} frames "
                                          f"(>{duration_tolerance})", a, b)
    return SequenceDiff(True)


@dataclass
class PixelDiff:
    """The result of comparing two timelines frame-for-frame by RGB digest."""
    ok: bool
    frame: Optional[int] = None       # first frame whose RGB digest differs
    screen_ref: str = ""
    screen_cand: str = ""
    sha_ref: str = ""
    sha_cand: str = ""
    compared: int = 0                 # how many frames were compared before the first diff (or in total)


def diff_pixels(ref: "list[FrameRecord]", cand: "list[FrameRecord]") -> PixelDiff:
    """Compare two timelines frame-for-frame by RGB digest. The first frame whose digest differs (or a length
    mismatch) is the divergence. This is the strong proof — identical pixels AND identical cadence."""
    n = min(len(ref), len(cand))
    for i in range(n):
        if ref[i].rgb_sha != cand[i].rgb_sha:
            return PixelDiff(False, i, ref[i].screen, cand[i].screen, ref[i].rgb_sha, cand[i].rgb_sha, i)
    if len(ref) != len(cand):
        i = n
        a = ref[i] if i < len(ref) else None
        b = cand[i] if i < len(cand) else None
        return PixelDiff(False, i, a.screen if a else "", b.screen if b else "",
                         a.rgb_sha if a else "", b.rgb_sha if b else "", n)
    return PixelDiff(True, None, compared=n)


def format_sequence(runs: "list[ScreenRun]") -> str:
    """A one-line human-readable rendering of a screen sequence: ``screenxCOUNT -> screenxCOUNT -> ...``."""
    return " -> ".join(f"{r.screen}x{r.count}" for r in runs)
