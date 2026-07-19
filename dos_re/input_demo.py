"""Reusable deterministic input-demo recording and playback for DOS runtimes.

The recorder stores a start snapshot plus VM-visible input events keyed by an
emulated boundary counter.  It intentionally does not know about SDL,
video modes, or a particular game.  Front-ends provide a demo name, metadata,
and the boundary at which host input is delivered.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # type-only: see the note below
    from .runtime import Runtime

# This module is a DATA layer -- it reads and writes demo JSON -- and is kept
# CPU-FREE so a CPU-free backend can record and replay demos.  (The CPUless
# runtime records every session so a crash can be reproduced; importing a
# CPU-carrying module here would breach its hard wall.)  The three edges that
# used to reach the interpreter are all avoidable and now resolve lazily:
#   * ``Runtime``          -- type hints only, so TYPE_CHECKING above;
#   * ``write_snapshot``   -- only on the branches that write a start snapshot;
#   * ``deliver_scancode`` -- only the DEFAULT delivery, so it is resolved when
#     a caller does not supply its own ``deliver`` (the CPU-free callers do).
def _default_deliver(rt, scancode: int) -> None:
    from .interrupts import deliver_scancode
    deliver_scancode(rt, scancode)

# v2 adds "mouse" events (normalized u/v position + Microsoft button mask,
# applied via ``rt.dos.set_mouse_norm`` on replay).  v1 keyboard-only demos
# load and replay unchanged -- the loader accepts 1..DEMO_VERSION.
DEMO_VERSION = 2


@dataclass(frozen=True)
class InputDemoEvent:
    boundary: int
    seq: int
    kind: str
    value: int | None = None
    scancode: int | None = None
    text: str = ""
    # "mouse" events: window-normalized position (0.0..1.0) + button mask
    # (bit0=left, bit1=right, bit2=middle), the exact set_mouse_norm arguments.
    u: float | None = None
    v: float | None = None
    buttons: int | None = None

    @classmethod
    def from_json(cls, raw: dict) -> "InputDemoEvent":
        return cls(
            boundary=max(0, int(raw.get("boundary", 0))),
            seq=max(0, int(raw.get("seq", 0))),
            kind=str(raw.get("kind", "")),
            value=None if raw.get("value") is None else int(raw["value"]) & 0xFFFF,
            scancode=None if raw.get("scancode") is None else int(raw["scancode"]) & 0xFF,
            text=str(raw.get("text", "")),
            # `mx`/`my`/`value` are a LEGACY mouse encoding (a port that grew the
            # channel before this schema existed).  Read them as u/v/buttons so
            # those recordings still replay: the values pass through the same
            # set_mouse_norm clamp they always did, so replay is byte-identical
            # to what the recording was made under.  New demos only ever write
            # u/v/buttons.
            u=(None if raw.get("u") is None else float(raw["u"])) if raw.get("u") is not None
              else (None if raw.get("mx") is None else float(raw["mx"])),
            v=(None if raw.get("v") is None else float(raw["v"])) if raw.get("v") is not None
              else (None if raw.get("my") is None else float(raw["my"])),
            buttons=(int(raw["buttons"]) & 0xFF) if raw.get("buttons") is not None
                    else (int(raw["value"]) & 0xFF
                          if raw.get("kind") == "mouse" and raw.get("value") is not None
                          else None),
        )

    def to_json(self) -> dict:
        out: dict[str, int | float | str] = {"boundary": self.boundary, "seq": self.seq, "kind": self.kind}
        if self.value is not None:
            out["value"] = self.value & 0xFFFF
        if self.scancode is not None:
            out["scancode"] = self.scancode & 0xFF
        if self.text:
            out["text"] = self.text
        if self.u is not None:
            out["u"] = self.u
        if self.v is not None:
            out["v"] = self.v
        if self.buttons is not None:
            out["buttons"] = self.buttons & 0xFF
        return out


def mouse_sample(u: float, v: float, buttons: int) -> tuple[float, float, int]:
    """Quantize a host mouse state to exactly what a demo stores.

    u/v are clamped to 0.0..1.0 and rounded to 4 decimals; buttons is masked to
    a byte.  The front-end must apply THIS rounded sample to the VM while
    recording, never the full-precision host value: the game maps the
    normalized mouse onto a pixel and is sensitive to <1e-4 differences, so
    applying full precision while recording a rounded value makes the replay
    land on a different pixel and diverge (the PM recorder learned this the
    hard way — see pm_player).
    """
    u = 0.0 if u < 0 else (1.0 if u > 1 else u)
    v = 0.0 if v < 0 else (1.0 if v > 1 else v)
    return (round(u, 4), round(v, 4), int(buttons) & 0xFF)


class InputDemoRecorder:
    """Record a start snapshot plus VM-visible keyboard and mouse events.

    ``name`` is only used for the output directory prefix.  ``metadata`` is
    copied verbatim into the manifest so a game front-end can record things
    like video mode, sound mode, command tail, or executable identity without
    making the demo format game-specific.
    """

    def __init__(
        self,
        *,
        root: Path,
        name: str,
        metadata: dict[str, object] | None = None,
        snapshot_name: str = "snapshot",
    ) -> None:
        self.root = Path(root)
        self.name = _safe_demo_name(name)
        self.metadata = dict(metadata or {})
        self.snapshot_name = snapshot_name
        self.demo_dir: Path | None = None
        self.snapshot_dir: Path | None = None
        self.start_boundary = 0
        self._seq = 0
        self._events: list[InputDemoEvent] = []
        self._last_mouse: tuple[float, float, int] | None = None
        self._started_at = ""
        self._stopped_at = ""

    @property
    def active(self) -> bool:
        return self.demo_dir is not None

    @property
    def event_count(self) -> int:
        return len(self._events)

    def start(self, rt: Runtime, *, boundary: int, write_start_snapshot: bool = True) -> Path:
        """Begin recording.  ``write_start_snapshot=False`` records a COLD-START demo: no start snapshot is
        written and the manifest's ``snapshot`` is null, so playback boots a fresh runtime (from the boot
        params in ``metadata``) and replays from ``boundary`` 0 -- the input-only capture of a whole session
        from power-on.  Record such a demo at boundary 0 of a fresh boot so replay stays frame-aligned."""
        if self.active:
            raise RuntimeError("input demo recording is already active")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.demo_dir = self.root / f"demo_{self.name}_{stamp}"
        self.demo_dir.mkdir(parents=True, exist_ok=True)
        self.start_boundary = max(0, int(boundary))
        self._seq = 0
        self._events.clear()
        self._last_mouse = None
        self._started_at = datetime.now().isoformat(timespec="seconds")
        self._stopped_at = ""
        if write_start_snapshot:
            self.snapshot_dir = self.demo_dir / self.snapshot_name
            from .snapshot import write_snapshot
            write_snapshot(rt, self.snapshot_dir, status="input demo start snapshot",
                           steps=rt.cpu.instruction_count, trace_tail=())
        else:
            self.snapshot_name = None   # cold start: replay boots a fresh runtime, no snapshot
            self.snapshot_dir = None
        self._write_manifest(final=False)
        return self.demo_dir

    def record_scan(self, *, boundary: int, scancode: int) -> None:
        if not self.active:
            return
        self._append(InputDemoEvent(boundary=self._relative_boundary(boundary), seq=self._seq, kind="scan", value=scancode & 0xFF))

    def record_mouse(self, *, boundary: int, u: float, v: float, buttons: int) -> tuple[float, float, int]:
        """Record one mouse sample (normalized position + button mask) at ``boundary``.

        Returns the quantized sample the caller must apply to the VM (via
        ``rt.dos.set_mouse_norm``) so the recording and its replay set the exact
        same driver state.  Identical consecutive samples are deduped — call
        this once per boundary and a still mouse adds no events.
        """
        sample = mouse_sample(u, v, buttons)
        if not self.active or sample == self._last_mouse:
            return sample
        self._last_mouse = sample
        self._append(InputDemoEvent(
            boundary=self._relative_boundary(boundary),
            seq=self._seq,
            kind="mouse",
            u=sample[0],
            v=sample[1],
            buttons=sample[2],
        ))
        return sample

    def record_dos_key(self, *, boundary: int, scancode: int, text: str, value: int) -> None:
        if not self.active:
            return
        self._append(InputDemoEvent(
            boundary=self._relative_boundary(boundary),
            seq=self._seq,
            kind="dos_key",
            value=value & 0xFFFF,
            scancode=scancode & 0xFF,
            text=text[:1],
        ))

    def stop(self, *, boundary: int) -> Path:
        if not self.active or self.demo_dir is None:
            raise RuntimeError("input demo recording is not active")
        self._stopped_at = datetime.now().isoformat(timespec="seconds")
        self._write_manifest(final=True, end_boundary=self._relative_boundary(boundary))
        out = self.demo_dir
        self.demo_dir = None
        self.snapshot_dir = None
        return out

    def _relative_boundary(self, boundary: int) -> int:
        return max(0, int(boundary) - self.start_boundary)

    def _append(self, event: InputDemoEvent) -> None:
        self._events.append(event)
        self._seq += 1
        self._write_manifest(final=False)

    def _write_manifest(self, *, final: bool, end_boundary: int | None = None) -> None:
        if self.demo_dir is None:
            return
        manifest = {
            "version": DEMO_VERSION,
            "status": "complete" if final else "recording",
            "created_at": self._started_at,
            "stopped_at": self._stopped_at,
            "snapshot": self.snapshot_name,
            "metadata": self.metadata,
            "start_boundary": 0,
            "end_boundary": end_boundary,
            "event_count": len(self._events),
            "events": [event.to_json() for event in self._events],
        }
        (self.demo_dir / "input_demo.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class InputDemoPlayback:
    """Replay a recorded input demo into one or more runtimes."""

    def __init__(self, *, demo_dir: Path, manifest: dict) -> None:
        self.demo_dir = demo_dir
        self.manifest = manifest
        self.events = sorted((InputDemoEvent.from_json(raw) for raw in manifest.get("events", [])), key=lambda e: (e.boundary, e.seq))
        self._index = 0
        self._last_mouse: tuple[float, float, int] | None = None

    @classmethod
    def load(cls, path: str | Path) -> "InputDemoPlayback":
        p = Path(path)
        if p.is_dir():
            manifest_path = p / "input_demo.json"
            demo_dir = p
        else:
            manifest_path = p
            demo_dir = p.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = int(manifest.get("version", 0))
        if not 1 <= version <= DEMO_VERSION:
            raise ValueError(f"unsupported input demo version: {manifest.get('version')!r}")
        return cls(demo_dir=demo_dir, manifest=manifest)

    @property
    def is_cold_start(self) -> bool:
        """A cold-start demo has no start snapshot (manifest ``snapshot`` is null): playback boots a fresh
        runtime from the recorded boot params and replays from boundary 0 -- a whole session from power-on."""
        return self.manifest.get("snapshot") is None

    def snapshot_path(self) -> Path:
        snap = self.manifest.get("snapshot")
        if snap is None:
            raise ValueError(
                "cold-start demo has no start snapshot; boot a fresh runtime and replay "
                "(check .is_cold_start first)")
        path = Path(str(snap))
        if not path.is_absolute():
            path = self.demo_dir / path
        return path

    @property
    def next_event_index(self) -> int:
        """Index of the first recorded event that has not yet been replayed."""
        return self._index

    def reset(self) -> None:
        self._index = 0
        self._last_mouse = None

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self.events)

    @property
    def end_boundary(self) -> int | None:
        """Boundary at which recording stopped, if the manifest recorded one.

        Older demos predate the field; callers fall back to :attr:`exhausted`.
        """
        raw = self.manifest.get("end_boundary")
        return None if raw is None else max(0, int(raw))

    @property
    def has_mouse_events(self) -> bool:
        """Whether this recording carries any mouse input at all."""
        return any(e.kind == "mouse" for e in self.events)

    @property
    def mouse_present_hint(self) -> bool:
        """Whether INT 33h should report a mouse PRESENT when replaying this demo.

        Detecting a mouse changes a game's startup control flow (it enables
        pointer control), so replay must reproduce whatever the RECORDING was
        made under or it silently diverges. The recorder pins the answer in the
        manifest (``metadata.mouse_present``); demos predating that field fall
        back to "did it record any mouse input" -- which is exactly right for a
        keyboard-only recording, and conservative for a mouse one that happened
        to sit still. Front-ends set ``rt.dos.mouse_present`` from this.
        """
        raw = self.manifest.get("metadata", {}).get("mouse_present")
        if raw is not None:
            return bool(raw)
        return self.has_mouse_events

    def finished(self, boundary: int) -> bool:
        """Whether replay has reached the end of the recorded demo.

        Prefer the recorded ``end_boundary`` so trailing idle frames (recorded
        after the last key event) still play back before stopping; fall back to
        "all events applied" for demos that have no end boundary.
        """
        end = self.end_boundary
        if end is not None:
            return boundary >= end
        return self.exhausted

    def apply_to_runtime(self, boundary: int, rt: Runtime, *, deliver: Callable[[Runtime, int], None] = _default_deliver, single: bool = False) -> int:
        return self.apply_to_runtimes(boundary, (rt,), deliver=deliver, single=single)

    def apply_to_runtimes(self, boundary: int, runtimes: Sequence[Runtime], *, deliver: Callable[[Runtime, int], None] = _default_deliver, single: bool = False) -> int:
        """Deliver due demo events (boundary <= ``boundary``) to each runtime.

        With ``single=True`` deliver **at most one** event this call.  Callers pass
        this when the game is parked in a fine-grained menu keyboard-poll wait, so
        several events recorded against the *same* boundary (a key release followed
        by a re-press, or one arrow's release sharing a boundary with the next
        arrow's press) are spread across successive poll iterations.  Otherwise the
        game samples the keyboard only once after all of them are applied, never
        observes the intermediate release, and collapses two taps into one --
        defeating the original release-debounce loops.  Real frame boundaries
        (``single=False``) deliver every due event, matching once-per-frame sampling.
        """
        boundary = max(0, int(boundary))
        applied = 0
        while self._index < len(self.events) and self.events[self._index].boundary <= boundary:
            event = self.events[self._index]
            if event.kind == "mouse":
                if event.u is None or event.v is None or event.buttons is None:
                    raise ValueError("mouse demo event missing u/v/buttons")
                self._last_mouse = (event.u, event.v, event.buttons)
            else:
                for rt in runtimes:
                    self._apply_event(rt, event, deliver=deliver)
            self._index += 1
            applied += 1
            if single:
                break
        # Re-apply the mouse EVERY call, not just when a mouse event is due:
        # set_mouse_norm re-maps the normalized position through the game's
        # CURRENT INT 33h range (AX=7/8), and a game may narrow that range while
        # the mouse is still.  Applying only on change would leave mouse_x/y
        # mapped with the OLD range and diverge from the recording, whose
        # front-end re-applies the sample every boundary (the proven PM design;
        # see pm_player's replay note).  Idempotent, so extra calls at the same
        # boundary (single-event poll waits) are harmless.
        if self._last_mouse is not None:
            u, v, buttons = self._last_mouse
            for rt in runtimes:
                set_mouse = getattr(rt.dos, "set_mouse_norm", None)
                if set_mouse is not None:
                    set_mouse(u, v, buttons)
        return applied

    @staticmethod
    def _apply_event(rt: Runtime, event: InputDemoEvent, *, deliver: Callable[[Runtime, int], None]) -> None:
        if event.kind == "scan":
            if event.value is None:
                raise ValueError("scan demo event missing value")
            deliver(rt, event.value & 0xFF)
        elif event.kind == "dos_key":
            if event.value is None:
                raise ValueError("dos_key demo event missing value")
            rt.dos.key_queue.append(event.value & 0xFFFF)
        elif event.kind == "mouse":
            # Re-inject the recorded sample through the INT 33h driver, exactly
            # as the recorder applied it (record_mouse returns the quantized
            # sample the recorder feeds to set_mouse_norm), so replay reproduces
            # the driver state bit-for-bit. A runtime whose DOS core has no mouse
            # (an older/other platform adapter) simply ignores the channel.
            if event.u is None or event.v is None:
                raise ValueError("mouse demo event missing u/v")
            set_mouse = getattr(rt.dos, "set_mouse_norm", None)
            if set_mouse is not None:
                set_mouse(event.u, event.v, event.buttons)
        else:
            raise ValueError(f"unknown input demo event kind: {event.kind!r}")


def bios_key_value_from_scancode(scancode: int, text: str) -> int | None:
    if not text:
        text = {
            0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5", 0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
            0x0C: "-", 0x0D: "=", 0x0E: "\b", 0x0F: "\t",
            0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t", 0x15: "y", 0x16: "u", 0x17: "i", 0x18: "o", 0x19: "p",
            0x1A: "[", 0x1B: "]", 0x1C: "\r",
            0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g", 0x23: "h", 0x24: "j", 0x25: "k", 0x26: "l", 0x27: ";",
            0x28: "'", 0x29: "`", 0x2B: "\\",
            0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v", 0x30: "b", 0x31: "n", 0x32: "m", 0x33: ",", 0x34: ".", 0x35: "/",
            0x39: " ", 0x01: "\x1b",
        }.get(scancode & 0xFF, "")
    if not text:
        return None
    ch = ord(text[0])
    if ch < 0x20 and ch not in (0x08, 0x09, 0x0D, 0x1B):
        return None
    return (((scancode & 0xFF) << 8) | (ch & 0xFF)) & 0xFFFF


# Backwards-compatible alias used by existing front-ends/tests.
dos_key_value = bios_key_value_from_scancode


def _safe_demo_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(name).strip())
    return cleaned or "input"
