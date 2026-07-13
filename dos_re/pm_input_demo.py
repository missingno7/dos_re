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

import hashlib
import json
from pathlib import Path

from .lift.runtime32 import interp_one32


def frame_digest(cpu) -> str:
    """A short, game-agnostic fingerprint of the full VM state at a frame seam.

    With the deterministic clock the entire flat memory is reproducible, so
    hashing it detects ANY divergence between a recording and its replay.  Kept
    short (8 bytes) — enough to catch drift, cheap to store per frame."""
    return hashlib.sha1(cpu.mem.data).hexdigest()[:16]

# A PM demo is a self-contained BUNDLE directory (not a lone file), exactly
# like the real-mode player's demos: a start snapshot the replay boots from,
# plus the input manifest keyed to it.  This makes a demo deterministic and
# game-agnostic — the same "record here, replay from here" flow for every
# title, with snapshots/demos an internal detail the user never has to wire up.
DEMO_VERSION = 1
INPUT_JSON = "input_demo.json"        # the manifest inside the bundle
SNAPSHOT_NAME = "snapshot"            # the start-snapshot subdir inside the bundle


class PMInputDemo:
    """A recorded input timeline: events tagged with a 0-based frame index,
    plus the name of the start snapshot the replay boots from."""

    def __init__(self, frame_tick_addr: int | None = None):
        self.frame_tick_addr = frame_tick_addr
        self.events: list = []        # [frame, kind, payload]
        self.total_frames = 0
        self.snapshot: str | None = None   # start-snapshot subdir name (None = cold start)
        self.metadata: dict = {}
        self.digests: dict[int, str] = {}  # frame index -> frame_digest(cpu) at record time

    def add(self, frame: int, kind: str, payload) -> None:
        self.events.append([int(frame), kind, payload])

    def by_frame(self) -> dict:
        m: dict[int, list] = {}
        for frame, kind, payload in self.events:
            m.setdefault(frame, []).append((kind, payload))
        return m

    def _manifest(self, status: str) -> dict:
        return {
            "version": DEMO_VERSION,
            "status": status,
            "frame_tick_addr": self.frame_tick_addr,
            "snapshot": self.snapshot,
            "total_frames": self.total_frames,
            "metadata": self.metadata,
            "events": self.events,
            "digests": {str(k): v for k, v in self.digests.items()},
        }

    def write_manifest(self, bundle_dir: str | Path, *, status: str) -> Path:
        """Write ``input_demo.json`` into the bundle directory."""
        d = Path(bundle_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / INPUT_JSON
        p.write_text(json.dumps(self._manifest(status), indent=2))
        return p

    def save(self, path: str | Path) -> Path:
        """Back-compat: write just the manifest to an explicit path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self._manifest("complete"), indent=2))
        return p

    @classmethod
    def load(cls, path: str | Path) -> "PMInputDemo":
        """Load a demo from a bundle directory (reads ``input_demo.json``) or,
        for back-compat, directly from a manifest JSON file."""
        p = Path(path)
        manifest = p / INPUT_JSON if p.is_dir() else p
        d = json.loads(manifest.read_text())
        o = cls(d.get("frame_tick_addr"))
        o.events = d["events"]
        o.total_frames = d.get("total_frames", 0)
        o.snapshot = d.get("snapshot")
        o.metadata = d.get("metadata", {})
        o.digests = {int(k): v for k, v in d.get("digests", {}).items()}
        return o

    @classmethod
    def snapshot_dir(cls, path: str | Path) -> Path | None:
        """The start-snapshot directory inside a demo bundle, or None if the
        demo is a cold-start (no snapshot) or a legacy lone-JSON demo."""
        p = Path(path)
        if not p.is_dir():
            return None
        manifest = p / INPUT_JSON
        if not manifest.exists():
            return None
        name = json.loads(manifest.read_text()).get("snapshot")
        if not name:
            return None
        snap = p / name
        return snap if snap.exists() else None


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
