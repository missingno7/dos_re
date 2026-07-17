"""Pointer input maps through the LETTERBOX, not the window.

A pointer-driven game needs the position in the FRAME's own space.  The frame
is letterboxed whenever the window aspect differs from the frame's -- every
fullscreen window and every phone screen -- so a window-relative mapping both
skews the cursor (wrong scale) and offsets it (by the bar size).  The Display
owns the letterbox math, so it owns the inverse too: ports must not re-derive
it (that is how the bug shipped in the first place -- correct only because the
desktop window happened to match the frame aspect).
"""
from __future__ import annotations

import pytest

pytest.importorskip("pygame")
pytest.importorskip("numpy")

from dos_re.display import Display  # noqa: E402


class _FakeDisplay(Display):
    """Display without a window: exercise the pure geometry only."""

    def __init__(self, size):
        self.integer_scale = False
        self.par = 1.0
        self.gpu = False
        self._srcsurf = None
        self._texsize = None
        self._tex = None
        self._ov = {}
        self._last_rect = None
        self._size = size

    def get_size(self):
        return self._size


def _drawn(win, frame):
    d = _FakeDisplay(win)
    d._last_rect = d.letterbox(*frame)
    return d


def test_no_frame_drawn_yet_has_nothing_to_map_against():
    assert _FakeDisplay((800, 600)).window_to_frame_norm((10, 10)) is None


def test_exact_aspect_window_maps_corner_to_corner():
    d = _drawn((640, 400), (320, 200))            # the desktop case: no bars
    assert d.window_to_frame_norm((0, 0)) == (0.0, 0.0)
    assert d.window_to_frame_norm((639, 399)) == (1.0, 1.0)


def test_pillarboxed_window_ignores_the_bars():
    # A phone screen: 1600x1000 window, 320x200 frame -> frame is 1600 wide,
    # so it fits exactly... use a wider window to force pillarbox.
    d = _drawn((2000, 1000), (320, 200))          # frame scales to 1600x1000
    r = d._last_rect
    assert r.w == 1600 and r.h == 1000 and r.x == 200
    # the frame's left edge is at window x=200, NOT x=0
    assert d.window_to_frame_norm((200, 0)) == (0.0, 0.0)
    assert d.window_to_frame_norm((1799, 999)) == (1.0, 1.0)
    # centre of the frame is the centre of the frame, not of the window
    u, v = d.window_to_frame_norm((r.x + r.w // 2, r.y + r.h // 2))
    assert u == pytest.approx(0.5, abs=0.001)
    assert v == pytest.approx(0.5, abs=0.001)


def test_letterboxed_window_ignores_the_bars():
    d = _drawn((640, 800), (320, 200))            # tall window -> top/bottom bars
    r = d._last_rect
    assert r.h == 400 and r.y == 200
    assert d.window_to_frame_norm((0, 200)) == (0.0, 0.0)
    assert d.window_to_frame_norm((639, 599)) == (1.0, 1.0)


def test_a_touch_on_the_bar_clamps_into_the_frame():
    # A finger landing on a black bar must not drive the cursor off-frame.
    d = _drawn((2000, 1000), (320, 200))
    assert d.window_to_frame_norm((0, 500))[0] == 0.0        # left bar
    assert d.window_to_frame_norm((1999, 500))[0] == 1.0     # right bar


def test_window_mapping_would_be_wrong_here():
    # Pins WHY this exists: the naive window-relative mapping puts the frame's
    # left edge at u=0.1 instead of 0.0 -- a visible cursor offset on a phone.
    d = _drawn((2000, 1000), (320, 200))
    naive_u = d._last_rect.x / (2000 - 1)
    assert naive_u > 0.09
    assert d.window_to_frame_norm((d._last_rect.x, 500))[0] == 0.0
