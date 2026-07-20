"""Protected-mode input and frame-boundary adapters for ReplayArtifact.

Protected-mode execution needs a game-supplied frame seam, but it does not
need or own another replay format.  ``FrameClock`` supplies stable points and
``ProtectedModeInputAdapter`` applies normalized immutable replay events.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from .replay_input import MOUSE_CHANNEL, mouse_sample
from .lift.runtime32 import interp_one32
from .replay import ReplayEvent

KEY_CHANNEL = "protected-mode.key"


class ProtectedModeInputAdapter:
    """Apply immutable ReplayArtifact input events to a DOS/4GW host."""

    def __init__(self, events: Sequence[ReplayEvent], *, event_cursor: int = 0):
        self.events = tuple(events)
        self._cursor = 0
        self._last_mouse: tuple[float, float, int] | None = None
        self.seek(event_cursor)

    @property
    def event_cursor(self) -> int:
        return self._cursor

    def seek(self, event_cursor: int) -> None:
        cursor = int(event_cursor)
        if not 0 <= cursor <= len(self.events):
            raise ValueError("event cursor lies outside the replay event stream")
        self._cursor = cursor
        self._last_mouse = None
        for event in self.events[:cursor]:
            if event.channel == MOUSE_CHANNEL:
                self._last_mouse = _mouse(event)

    def apply(
        self, ordinal: int, dos, *,
        deliver_key: Callable[[object, str, bool], None],
    ) -> int:
        ordinal = max(0, int(ordinal))
        applied = 0
        while (
            self._cursor < len(self.events)
            and self.events[self._cursor].point.ordinal <= ordinal
        ):
            event = self.events[self._cursor]
            if event.channel == KEY_CHANNEL:
                if not isinstance(event.payload, dict):
                    raise ValueError("protected-mode key payload must be an object")
                deliver_key(dos, str(event.payload["name"]), bool(event.payload["make"]))
            elif event.channel == MOUSE_CHANNEL:
                self._last_mouse = _mouse(event)
            else:
                raise ValueError(
                    f"unsupported protected-mode replay channel: {event.channel!r}")
            self._cursor += 1
            applied += 1
        if self._last_mouse is not None:
            dos.set_mouse_norm(*self._last_mouse)
        return applied


def key_payload(name: str, make: bool) -> dict[str, object]:
    return {"name": str(name), "make": bool(make)}


def _mouse(event: ReplayEvent) -> tuple[float, float, int]:
    if not isinstance(event.payload, dict):
        raise ValueError("mouse replay event payload must be an object")
    try:
        return mouse_sample(
            float(event.payload["u"]), float(event.payload["v"]),
            int(event.payload["buttons"]))
    except KeyError as exc:
        raise ValueError(f"mouse replay event is missing {exc.args[0]!r}") from exc


class FramePaced(Exception):
    """Stop CPU execution exactly before the requested frame boundary."""


class FrameClock:
    """Adapter-defined stable frame-point counter."""

    def __init__(self, cpu, addr: int, on_frame):
        self.cpu = cpu
        self.addr = int(addr)
        self.on_frame = on_frame
        self.frame = 0
        self.stop_at = None
        cpu.replacement_hooks[self.addr] = self._tick
        cpu.hook_names[self.addr] = "frame_clock"

    def _tick(self, cpu) -> None:
        if self.stop_at is not None and self.frame >= self.stop_at:
            raise FramePaced()
        self.on_frame(self.frame)
        self.frame += 1
        interp_one32(cpu, self.addr)

    def remove(self) -> None:
        self.cpu.replacement_hooks.pop(self.addr, None)
        self.cpu.hook_names.pop(self.addr, None)
