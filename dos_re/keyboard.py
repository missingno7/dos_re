"""Frame-accurate translation of physical key events into scan-code delivery.

Some DOS games poll their key-state table once per rendered frame.  A key
therefore has to be *held down for at least one full frame* to be observed.  A
quick tap can deliver its press and release between two frames; if both are
applied before the frame runs, the key is set and cleared before the game ever
polls it and the press is silently lost.

``KeyDispatcher`` sits between the UI (which posts raw key up/down events from any
thread) and the interpreter (which calls :meth:`pump` once per frame).  It
delivers a make code as soon as a key goes down and defers the matching break
until the key has been held for at least one frame, so every tap is seen.
"""
from __future__ import annotations

import collections
from typing import Callable

#: Dedicated power-on INT 09h (IRQ1) entry.  IVT[9] points here so a game that
#: saves and chains to "the previous keyboard ISR" reaches the native BIOS
#: keyboard handler installed at this address; F000:E987 is the classic IBM BIOS
#: INT 9 entry point.  It lives in this CPU-FREE leaf (and is re-exported by
#: ``runtime_core``) because deciding whether a game installed its OWN INT 09h --
#: ``read IVT[9] != BIOS_INT9_ENTRY`` -- is exactly what a keyboard front-end must
#: do, including the CPUless backend, which cannot import a CPU-carrying module.
BIOS_INT9_ENTRY = (0xF000, 0xE987)


class KeyDispatcher:
    def __init__(self, deliver: Callable[[int], None]) -> None:
        # ``deliver`` is called with an XT scan code (make, or make|0x80 for break).
        self._deliver = deliver
        self._events: "collections.deque[tuple[str, int]]" = collections.deque()
        self._down: dict[int, int] = {}   # scancode -> frames held so far
        self._release: set[int] = set()   # scancodes with a release pending

    # Posted from the UI thread; deque ops are atomic under the GIL.
    def post_down(self, scancode: int) -> None:
        self._events.append(("down", scancode & 0xFF))

    def post_up(self, scancode: int) -> None:
        self._events.append(("up", scancode & 0xFF))

    def _drain_events(self) -> None:
        while self._events:
            kind, sc = self._events.popleft()
            if kind == "down":
                self._release.discard(sc)      # a re-press cancels a pending release
                if sc not in self._down:
                    self._deliver(sc)          # make code
                    self._down[sc] = 0
            else:
                self._release.add(sc)

    def _release_ready(self, *, hold_new_taps: bool) -> None:
        for sc in list(self._release):
            if self._down.get(sc, -1) >= 1 or not hold_new_taps:
                self._deliver(sc | 0x80)       # break code
                self._down.pop(sc, None)
                self._release.discard(sc)

    def pump_events(self) -> None:
        """Apply queued physical events without advancing the game-frame age.

        The interactive runner uses this during long no-frame loading bursts so
        a key released by the user does not remain logically held until the next
        visible frame.  New down+up taps drained here are released immediately;
        frame-start ``pump()`` remains the path that guarantees a tap spans one
        complete game poll.
        """
        self._drain_events()
        self._release_ready(hold_new_taps=False)

    def pump(self, *, allow_release: bool = True) -> None:
        """Apply queued events for one emulated boundary.

        ``allow_release=False`` is used by the interactive player immediately
        after a visual presenter boundary. The original program can present
        the frame before it checks some one-shot keys such as Esc, so releasing
        a quick tap at that boundary would clear the game's key table before
        the original post-present input code can observe it.  We still drain
        new key-down events and age held keys; the matching break is simply kept
        pending until a later timer/no-frame boundary.
        """
        self._drain_events()
        # Only release keys that have already been held for a full frame, and
        # only at boundaries where the caller knows post-present input polling is
        # not still ahead of the VM.
        if allow_release:
            self._release_ready(hold_new_taps=True)
        for sc in self._down:
            self._down[sc] += 1


# pygame key -> XT scan code (make). Break = make | 0x80.
def scancode_table(pygame) -> dict[int, int]:
    k = pygame
    table = {
        k.K_ESCAPE: 0x01, k.K_MINUS: 0x0C, k.K_EQUALS: 0x0D, k.K_BACKSPACE: 0x0E,
        k.K_TAB: 0x0F, k.K_RETURN: 0x1C, k.K_LCTRL: 0x1D, k.K_RCTRL: 0x1D,
        k.K_LSHIFT: 0x2A, k.K_RSHIFT: 0x36, k.K_LALT: 0x38, k.K_RALT: 0x38,
        k.K_SPACE: 0x39, k.K_UP: 0x48, k.K_LEFT: 0x4B, k.K_RIGHT: 0x4D,
        k.K_DOWN: 0x50, k.K_COMMA: 0x33, k.K_PERIOD: 0x34, k.K_SLASH: 0x35,
        k.K_SEMICOLON: 0x27, k.K_QUOTE: 0x28, k.K_BACKQUOTE: 0x29,
        k.K_LEFTBRACKET: 0x1A, k.K_RIGHTBRACKET: 0x1B, k.K_BACKSLASH: 0x2B,
        k.K_HOME: 0x47, k.K_PAGEUP: 0x49, k.K_END: 0x4F, k.K_PAGEDOWN: 0x51,
        k.K_INSERT: 0x52, k.K_DELETE: 0x53,
    }
    for i, key in enumerate((k.K_1, k.K_2, k.K_3, k.K_4, k.K_5, k.K_6, k.K_7,
                             k.K_8, k.K_9, k.K_0)):
        table[key] = 0x02 + i
    for i, ch in enumerate("qwertyuiop"):
        table[getattr(k, f"K_{ch}")] = 0x10 + i
    for i, ch in enumerate("asdfghjkl"):
        table[getattr(k, f"K_{ch}")] = 0x1E + i
    for i, ch in enumerate("zxcvbnm"):
        table[getattr(k, f"K_{ch}")] = 0x2C + i
    for i in range(10):  # F1..F10
        table[getattr(k, f"K_F{i + 1}")] = 0x3B + i
    return table
