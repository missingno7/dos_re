"""Window controls shared by every viewer: drag-resize + Alt+Enter fullscreen.

These are general Display features (dos_re.display), used identically by the
real-mode player and the protected-mode backend, so they are tested once here.

Skips when the optional viewer deps (numpy + pygame) are absent; uses SDL's
dummy video driver — no window, real code path."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pygame")

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

from dos_re.display import Display, is_fullscreen_toggle  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pygame_init():
    pygame.init()
    yield
    pygame.quit()


def _ev(key, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=key, mod=mod)


def test_alt_enter_is_the_only_fullscreen_chord():
    assert is_fullscreen_toggle(_ev(pygame.K_RETURN, pygame.KMOD_LALT))
    assert is_fullscreen_toggle(_ev(pygame.K_RETURN, pygame.KMOD_RALT))
    # a bare Enter is game input, not a viewer chord — must not toggle
    assert not is_fullscreen_toggle(_ev(pygame.K_RETURN))
    assert not is_fullscreen_toggle(_ev(pygame.K_a, pygame.KMOD_LALT))
    assert not is_fullscreen_toggle(_ev(pygame.K_RETURN, pygame.KMOD_LCTRL))
    # key RELEASES are never the chord (the viewer swallows them separately)
    assert not is_fullscreen_toggle(
        pygame.event.Event(pygame.KEYUP, key=pygame.K_RETURN, mod=pygame.KMOD_LALT))


def test_resize_tracks_the_requested_size():
    d = Display((640, 480), title="test")
    d.resize(800, 600)
    assert d.get_size() == (800, 600)
    # absurdly small drags are clamped, not honoured
    d.resize(10, 10)
    w, h = d.get_size()
    assert w >= 160 and h >= 100


def test_fullscreen_toggle_round_trips_the_windowed_size():
    d = Display((640, 480), title="test")
    d.resize(800, 600)
    assert d.fullscreen is False
    assert d.toggle_fullscreen() is True
    assert d.fullscreen is True
    assert d.toggle_fullscreen() is False
    assert d.fullscreen is False
    assert d.get_size() == (800, 600)     # pre-fullscreen window restored


def test_opengl_fullscreen_uses_borderless_desktop_window():
    class Window:
        def __init__(self):
            self.calls = []
            self.size = (640, 480)

        def set_fullscreen(self, *, desktop):
            self.calls.append(("fullscreen", desktop))

        def set_windowed(self):
            self.calls.append(("windowed",))

    display = Display.__new__(Display)
    display.opengl = True
    display._gl_window = Window()
    display._texsize = (320, 200)

    display.set_fullscreen(True)
    assert display._gl_window.calls == [("fullscreen", True)]
    assert display._texsize is None

    display.set_fullscreen(False, windowed_size=(800, 600))
    assert display._gl_window.calls[-1] == ("windowed",)
    assert display._gl_window.size == (800, 600)


@pytest.mark.parametrize("fw,fh", [(320, 200), (320, 240), (320, 400)])
def test_par_shows_every_pm_geometry_at_4_3(fw, fh):
    """The protected-mode backend sets par = 3w/4h so mode 13h and both Mode X
    geometries letterbox to the 4:3 a real monitor showed, at any window size."""
    d = Display((800, 600), title="test")
    d.par = (3.0 * fw) / (4.0 * fh)
    for size in ((800, 600), (1280, 400), (500, 900)):
        d.resize(*size)
        r = d.letterbox(fw, fh)
        assert r.w / r.h == pytest.approx(4 / 3, abs=0.01)
        sw, sh = d.get_size()
        assert r.w <= sw and r.h <= sh          # fits inside the window
