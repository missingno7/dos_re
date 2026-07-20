"""Real-mode input normalization and application for ReplayArtifact events.

This module is deliberately not a replay format.  It owns no manifest,
snapshot, clock, version, or persistence.  The player records these normalized
channels into :class:`dos_re.replay.ReplayArtifact`; verification drivers use
the same adapter to apply them to oracle and candidate runtimes.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .replay import ReplayEvent

if TYPE_CHECKING:
    from .runtime import Runtime

SCAN_CHANNEL = "real-mode.scan"
DOS_KEY_CHANNEL = "real-mode.dos-key"
MOUSE_CHANNEL = "mouse.normalized"


def _default_deliver(rt: Runtime, scancode: int) -> None:
    from .interrupts import deliver_scancode
    deliver_scancode(rt, scancode)


def mouse_sample(u: float, v: float, buttons: int) -> tuple[float, float, int]:
    """Canonical host-independent mouse sample stored in replay payloads."""
    return (
        round(min(1.0, max(0.0, float(u))), 4),
        round(min(1.0, max(0.0, float(v))), 4),
        int(buttons) & 0x07,
    )


class RealModeInputAdapter:
    """Apply immutable ReplayArtifact input events to real-mode runtimes."""

    def __init__(self, events: Sequence[ReplayEvent], *, event_cursor: int = 0):
        self.events = tuple(events)
        self._cursor = 0
        self._last_mouse: tuple[float, float, int] | None = None
        self.seek(event_cursor)

    @property
    def event_cursor(self) -> int:
        return self._cursor

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self.events)

    def seek(self, event_cursor: int) -> None:
        cursor = int(event_cursor)
        if not 0 <= cursor <= len(self.events):
            raise ValueError("event cursor lies outside the replay event stream")
        self._cursor = cursor
        self._last_mouse = None
        for event in self.events[:cursor]:
            if event.channel == MOUSE_CHANNEL:
                self._last_mouse = _mouse_payload(event)

    def apply_to_runtime(
        self, ordinal: int, rt: Runtime, *,
        deliver: Callable[[Runtime, int], None] = _default_deliver,
        single: bool = False,
    ) -> int:
        return self.apply_to_runtimes(
            ordinal, (rt,), deliver=deliver, single=single)

    def apply_to_runtimes(
        self, ordinal: int, runtimes: Sequence[Runtime], *,
        deliver: Callable[[Runtime, int], None] = _default_deliver,
        single: bool = False,
    ) -> int:
        """Apply due events, optionally one at a time for input-poll waits."""
        ordinal = max(0, int(ordinal))
        applied = 0
        while (
            self._cursor < len(self.events)
            and self.events[self._cursor].point.ordinal <= ordinal
        ):
            event = self.events[self._cursor]
            if event.channel == MOUSE_CHANNEL:
                self._last_mouse = _mouse_payload(event)
            elif event.channel == SCAN_CHANNEL:
                scancode = _integer_payload(event, "scancode") & 0xFF
                for rt in runtimes:
                    deliver(rt, scancode)
            elif event.channel == DOS_KEY_CHANNEL:
                value = _integer_payload(event, "value") & 0xFFFF
                for rt in runtimes:
                    rt.dos.key_queue.append(value)
            else:
                raise ValueError(f"unsupported real-mode replay channel: {event.channel!r}")
            self._cursor += 1
            applied += 1
            if single:
                break
        # The game's INT 33h range can change without host mouse motion.
        if self._last_mouse is not None:
            for rt in runtimes:
                setter = getattr(rt.dos, "set_mouse_norm", None)
                if setter is not None:
                    setter(*self._last_mouse)
        return applied


def scan_payload(scancode: int) -> dict[str, int]:
    return {"scancode": int(scancode) & 0xFF}


def dos_key_payload(scancode: int, text: str, value: int) -> dict[str, object]:
    return {
        "scancode": int(scancode) & 0xFF,
        "text": str(text)[:1],
        "value": int(value) & 0xFFFF,
    }


def mouse_payload(u: float, v: float, buttons: int) -> dict[str, object]:
    u, v, buttons = mouse_sample(u, v, buttons)
    return {"u": u, "v": v, "buttons": buttons}


def _integer_payload(event: ReplayEvent, name: str) -> int:
    if not isinstance(event.payload, dict) or name not in event.payload:
        raise ValueError(f"{event.channel} replay event is missing {name!r}")
    return int(event.payload[name])


def _mouse_payload(event: ReplayEvent) -> tuple[float, float, int]:
    if not isinstance(event.payload, dict):
        raise ValueError("mouse replay event payload must be an object")
    try:
        return mouse_sample(
            float(event.payload["u"]), float(event.payload["v"]),
            int(event.payload["buttons"]))
    except KeyError as exc:
        raise ValueError(f"mouse replay event is missing {exc.args[0]!r}") from exc


def bios_key_value_from_scancode(scancode: int, text: str) -> int | None:
    """Translate one host key to the BIOS AX value expected by INT 16h."""
    if not text:
        text = {
            0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
            0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
            0x0C: "-", 0x0D: "=", 0x0E: "\b", 0x0F: "\t",
            0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t",
            0x15: "y", 0x16: "u", 0x17: "i", 0x18: "o", 0x19: "p",
            0x1A: "[", 0x1B: "]", 0x1C: "\r",
            0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g",
            0x23: "h", 0x24: "j", 0x25: "k", 0x26: "l", 0x27: ";",
            0x28: "'", 0x29: "`", 0x2B: "\\",
            0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v", 0x30: "b",
            0x31: "n", 0x32: "m", 0x33: ",", 0x34: ".", 0x35: "/",
            0x39: " ", 0x01: "\x1b",
        }.get(scancode & 0xFF, "")
    if not text:
        return None
    ch = ord(text[0])
    if ch < 0x20 and ch not in (0x08, 0x09, 0x0D, 0x1B):
        return None
    return (((scancode & 0xFF) << 8) | (ch & 0xFF)) & 0xFFFF
