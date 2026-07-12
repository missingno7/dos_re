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
    Duck-typed on purpose: ``tobytes()`` yields C-order bytes for a numpy array; bytes-like inputs are hashed
    directly — so both a rendered ndarray and a raw framebuffer slice digest identically."""
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


def filter_runs(runs: "list[ScreenRun]", ignore: "frozenset[str] | set[str]" = frozenset()) -> "list[ScreenRun]":
    """Drop TRANSITION-state runs and merge the adjacent same-screen runs they separated.

    A reference VM shows short transition states a native front end legitimately never renders as frames of
    their own — a black 'loading' head while a 13h image is being copied in, an 'other' frame mid mode-switch,
    a 'blanked' display during a palette load. Filtering them (and merging what they split) leaves the REAL
    screen order, which is the invariant to compare when the capture's frame cadence is not retrace-faithful
    (e.g. a workbench demo recorded under an instruction-budget clock inflates TIMED screens' durations)."""
    out: "list[ScreenRun]" = []
    for r in runs:
        if r.screen in ignore:
            continue
        if out and out[-1].screen == r.screen:
            out[-1] = ScreenRun(out[-1].screen, out[-1].start, out[-1].count + r.count)
        else:
            out.append(r)
    return out


def format_sequence(runs: "list[ScreenRun]") -> str:
    """A one-line human-readable rendering of a screen sequence: ``screenxCOUNT -> screenxCOUNT -> ...``."""
    return " -> ".join(f"{r.screen}x{r.count}" for r in runs)


# --------------------------------------------------------------------------------------------------------------
# The TRANSITION-STATE proof pieces (the front-end analogue of the tick demo's digest machinery). Developed on
# the first completed port's verify_native_frontend; the 4-gate pattern they compose is documented in
# docs/agent_toolbox.md §12b:  [1] screen ORDER  [2] a WITNESS byte-compared at every transition
# [3] entry-state equality outside an OWNED byte set  [4] the owned set proven INERT by dual replay.
# --------------------------------------------------------------------------------------------------------------

def pack_fields(data, fields, base: int = 0) -> bytes:
    """Pack a WITNESS from ``data``: ``fields`` = ((name, offset, size), ...) — the adapter's decision-state
    contract (the state the front end is FOR: chosen level/mode/lives/attract-flag/password...). Byte-compare
    the packed witness at every screen transition; a mismatch is a real behaviour divergence, cadence-free."""
    return b"".join(bytes(data[base + off:base + off + n]) for _, off, n in fields)


def diff_fields(a: bytes, b: bytes, fields) -> "list[str]":
    """Human-readable per-field diff of two :func:`pack_fields` witnesses (empty list = identical)."""
    out, pos = [], 0
    for name, _off, n in fields:
        va, vb = a[pos:pos + n], b[pos:pos + n]
        if va != vb:
            out.append(f"{name}: ref={va.hex()} cand={vb.hex()}")
        pos += n
    return out


def input_segments(filtered_runs: "list[ScreenRun]", per_frame_inputs: "list[bytes]", total_frames: int):
    """Split the reference's per-frame raw-input stream into PER-SCREEN segments.

    The reference and the candidate share no frame clock (a timed screen lasts a different number of frames on
    each), so absolute-index input injection desyncs after the first timed screen. Causal alignment instead:
    segment the input by the reference's LOGICAL screen runs — segment k = the input frames recorded while
    screen k was up (including any trailing transition frames, so nothing is lost). The candidate then consumes
    segment k while ITS OWN screen is runs[k].screen (see :class:`SegmentedInput`), so a keypress lands on the
    same screen at the same relative moment on both sides."""
    bounds = [r.start for r in filtered_runs] + [total_frames]
    return [(filtered_runs[j].screen, per_frame_inputs[bounds[j]:bounds[j + 1]])
            for j in range(len(filtered_runs))]


class SegmentedInput:
    """Feed :func:`input_segments` to a candidate front end, advancing causally with ITS screen changes.

    Call :meth:`next` every candidate frame with the candidate's current canonical screen (``None`` for a
    transition state): it returns the input bytes to inject for this frame. While the candidate stays on
    segment k's screen it consumes that segment frame-by-frame; when its screen becomes segment k+1's, the
    cursor jumps there (a timed screen the candidate finishes faster just skips the unused idle input — the
    presses recorded for LATER screens are still delivered on those screens). Exhausted segment = ``blank``
    (keys released)."""

    def __init__(self, segments, blank: bytes):
        self.segments = segments
        self.blank = blank
        self.k = 0
        self.c = 0

    def next(self, current_screen) -> bytes:
        if (current_screen is not None and self.k + 1 < len(self.segments)
                and current_screen == self.segments[self.k + 1][0]
                and current_screen != self.segments[self.k][0]):
            self.k += 1
            self.c = 0
        seg = self.segments[self.k][1] if self.k < len(self.segments) else ()
        buf = seg[self.c] if self.c < len(seg) else self.blank
        self.c += 1
        return buf


def diff_offsets(a, b) -> "list[int]":
    """All offsets where two equal-length buffers differ — the OWNED set for the inertness gate: the bytes
    where a candidate's entry state legitimately differs from the reference's (audio-driver data, load-layout
    pointers, scene scratch). Gate [4] then replays the recorded gameplay from BOTH states and requires every
    tick to stay byte-identical OUTSIDE this set (:func:`spread_beyond`) — proving the owned bytes inert
    rather than assuming them irrelevant."""
    return [o for o in range(min(len(a), len(b))) if a[o] != b[o]]


def spread_beyond(a, b, owned: "set[int] | frozenset[int]") -> "list[int]":
    """Offsets differing between ``a`` and ``b`` that are NOT in ``owned`` — the inertness violation set.
    Non-empty = the owned-region difference PROPAGATED into state both sides were supposed to compute
    identically; the first offsets localize the leak."""
    return [o for o in range(min(len(a), len(b))) if a[o] != b[o] and o not in owned]
