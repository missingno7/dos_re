"""Deterministic input demos for the PM (DOS/4GW) runtime.

Records input (keyboard make/break + one mouse sample per frame) keyed to the
game's own FRAME counter — an adapter-supplied ``frame_tick_addr`` that the
program executes once per frame (e.g. its per-frame update entry).  Keying to
the frame, not to wall-clock or instruction count, is what makes a demo
replay identically: however many times the game spins on the retrace during
live play, replay re-injects each frame's input at the same frame boundary.

The clock is installed as a replacement hook at ``frame_tick_addr``: it counts
the frame, fires a callback (record the mouse sample / inject this frame's
events), then runs the original entry instruction via ``interp_one32`` so the
frame proceeds normally.  Game-agnostic: the adapter supplies the address.
"""
from __future__ import annotations

import json
from pathlib import Path

from .lift.runtime32 import interp_one32


class PMInputDemo:
    """A recorded input timeline: events tagged with a 0-based frame index."""

    def __init__(self, frame_tick_addr: int | None = None):
        self.frame_tick_addr = frame_tick_addr
        self.events: list = []        # [frame, kind, payload]
        self.total_frames = 0

    def add(self, frame: int, kind: str, payload) -> None:
        self.events.append([int(frame), kind, payload])

    def by_frame(self) -> dict:
        m: dict[int, list] = {}
        for frame, kind, payload in self.events:
            m.setdefault(frame, []).append((kind, payload))
        return m

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "frame_tick_addr": self.frame_tick_addr,
            "total_frames": self.total_frames,
            "events": self.events,
        }))
        return p

    @classmethod
    def load(cls, path: str | Path) -> "PMInputDemo":
        d = json.loads(Path(path).read_text())
        o = cls(d.get("frame_tick_addr"))
        o.events = d["events"]
        o.total_frames = d.get("total_frames", 0)
        return o


class FramePaced(Exception):
    """Raised by the FrameClock to stop ``cpu.run`` exactly at a frame boundary.

    The tick that would enter frame ``stop_at`` raises this WITHOUT running the
    entry instruction, so ``cpu.eip`` stays on the frame-tick address — the run
    resumes into that frame on the next call.  Lets a viewer advance exactly
    one logical frame per present (correct game speed) instead of overshooting."""


class FrameClock:
    """Per-frame boundary counter installed at ``frame_tick_addr``.

    ``on_frame(frame_index)`` runs at the start of each frame, before the
    frame's own code — the record hook samples input there, the replay hook
    injects it there.  Set ``stop_at`` to have the clock break the run at that
    frame boundary (exact-frame pacing)."""

    def __init__(self, cpu, addr: int, on_frame):
        self.cpu = cpu
        self.addr = addr
        self.on_frame = on_frame
        self.frame = 0
        self.stop_at = None
        cpu.replacement_hooks[addr] = self._tick
        cpu.hook_names[addr] = "frame_clock"

    def _tick(self, cpu) -> None:
        if self.stop_at is not None and self.frame >= self.stop_at:
            raise FramePaced()            # break the run; eip stays on the tick address
        self.on_frame(self.frame)
        self.frame += 1
        interp_one32(cpu, self.addr)      # run the entry instruction; hook suppressed for it

    def remove(self) -> None:
        self.cpu.replacement_hooks.pop(self.addr, None)
        self.cpu.hook_names.pop(self.addr, None)
