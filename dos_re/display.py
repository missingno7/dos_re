"""Window + present backend for game viewers (optional; needs numpy + pygame).

Prefers a GPU-accelerated SDL2 renderer (``pygame._sdl2.video``): the small game frame (320x200, or a bit
wider in widescreen) is uploaded once per frame as a streaming texture and the GPU scales it to the window.
So present cost is ~constant regardless of window size — a 4K / fullscreen window is as cheap as a tiny one
(a software path that scales + flips the whole WINDOW surface every frame loses fps as the window grows).

Falls back to the software ``pygame.display`` surface path when the SDL2 renderer is unavailable, so nothing
regresses on odd setups. Both paths share the aspect-correct letterbox math and an ``integer_scale`` option.

This module is part of the FRONTEND RING (see tools/lint.py): it may import numpy/pygame, but the framework
core never imports it — only ``dos_re.player`` (and game viewers) load it, lazily, when a window opens.

Origin: copied from pre2_port's scripts/display.py (zero game knowledge; only the window title changed);
is owned directly by the package frontend ring.
"""
from __future__ import annotations

from dos_re.display_platform import configure_physical_pixel_coordinates

# This must run before pygame initializes SDL's video subsystem.  In
# particular, a 3840x2160 Windows desktop at 150% scaling must not be exposed
# as the DPI-virtualized 2560x1440 logical desktop.
configure_physical_pixel_coordinates()

import numpy as np
import pygame

_TITLE = "dos_re native viewer"


def is_fullscreen_toggle(event) -> bool:
    """True for the conventional Alt+Enter fullscreen chord.

    Shared by every viewer so the chord is identical across the real-mode and
    protected-mode players (and so each can also swallow the event instead of
    leaking a stray Enter into the guest as game input)."""
    return (event.type == pygame.KEYDOWN
            and event.key == pygame.K_RETURN
            and bool(event.mod & pygame.KMOD_ALT))


class Display:
    def __init__(self, size, *, title: str = _TITLE, opengl: bool = False):
        self.integer_scale = False
        self.fullscreen = False
        self._windowed_size = tuple(size)   # restored when leaving fullscreen
        self._windowed_position = None
        self._windowed_borderless = False
        self._windowed_always_on_top = False
        self._windowed_resizable = True
        self.par = 1.0                     # displayed pixel aspect (height/width). 1.0 = square pixels;
        #                                    1.2 = the DOS 4:3 look (320x200 shown at 4:3 -> pixels 1.2x tall).
        self.gpu = False
        self._srcsurf = None
        self._texsize = None
        self._tex = None
        # keyed source textures for the free-camera compositing path (upload_frame /
        # draw_textured): several differently-sized sources per frame (e.g. a wide
        # level texture + a small HUD texture), each cached under its own key.
        self._ktex = {}
        self._ksize = {}
        self._ksurf = {}
        self._last_rect = None             # where the last frame was drawn (see window_to_frame_norm)
        self._ov = {}                      # cached overlay textures keyed by id(surface)
        self.opengl = bool(opengl)
        self._gl_window = None
        self._opengl_flags = pygame.RESIZABLE | pygame.OPENGL | pygame.DOUBLEBUF
        if self.opengl:
            # A product-selected GPU presenter owns the OpenGL drawing.  Keep
            # the same Display geometry/input API, but do not create SDL's 2D
            # renderer on the same window/context.
            self.screen = pygame.display.set_mode(size, self._opengl_flags)
            pygame.display.set_caption(title)
            try:
                from pygame._sdl2 import video as sdl2
                # Control the window created by display.set_mode without
                # creating another window or an SDL 2D renderer.
                self._gl_window = sdl2.Window.from_display_module()
            except Exception:              # noqa: BLE001 - optional pygame API
                pass
            return
        try:
            from pygame._sdl2 import video as sdl2
            self._sdl2 = sdl2
            self.window = sdl2.Window(title, size=size, resizable=True)
            self.renderer = sdl2.Renderer(self.window, accelerated=-1, vsync=False)
            self.renderer.draw_color = (0, 0, 0, 255)
            self.gpu = True
        except Exception:                  # noqa: BLE001 — no GPU / no _sdl2 -> software surface
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)

    # --- geometry -------------------------------------------------------------------------------------
    def get_size(self):
        if self.opengl:
            # On pygame-ce/SDL, resizing an OPENGL window updates the native
            # drawable immediately while the legacy display Surface may retain
            # its creation size. Using Surface.get_size() therefore leaves a
            # stale viewport after every user resize.
            return tuple(pygame.display.get_window_size())
        return tuple(self.window.size) if self.gpu else self.screen.get_size()

    def letterbox(self, fw: int, fh: int) -> "pygame.Rect":
        """Aspect-correct destination rect for an fw×fh frame centred in the window (integer-snapped if set).
        ``par`` (pixel aspect, height/width) stretches the frame vertically so square-buffer content displays
        at the intended pixel shape: par=1.2 shows 320x200 at 4:3 (the DOS CRT look) instead of 1.6:1."""
        sw, sh = self.get_size()
        eh = fh * self.par                                   # effective (displayed) frame height in px units
        f = min(sw / fw, sh / eh)
        if self.integer_scale and f >= 1.0:
            f = float(int(f))
        tw, th = max(1, int(fw * f)), max(1, int(eh * f))
        return pygame.Rect((sw - tw) // 2, (sh - th) // 2, tw, th)

    # --- drawing --------------------------------------------------------------------------------------
    def draw_game(self, rgb) -> "pygame.Rect":
        """Draw one game frame (an H×W×3 uint8 array) scaled + letterboxed; returns its on-screen rect. Does
        NOT present — call flip() after any overlays."""
        arr = np.asarray(rgb, np.uint8)
        fh, fw = arr.shape[:2]
        rect = self.letterbox(fw, fh)
        if self.gpu:
            if self._texsize != (fw, fh):
                self._tex = self._sdl2.Texture(self.renderer, (fw, fh), streaming=True)
                self._srcsurf = pygame.Surface((fw, fh))
                self._texsize = (fw, fh)
            pygame.surfarray.blit_array(self._srcsurf, arr.swapaxes(0, 1))
            self._tex.update(self._srcsurf)
            self.renderer.clear()                                 # black letterbox bars
            self._tex.draw(dstrect=rect)
        else:
            if self._texsize != (fw, fh):
                self._srcsurf = pygame.Surface((fw, fh))
                self._texsize = (fw, fh)
            pygame.surfarray.blit_array(self._srcsurf, arr.swapaxes(0, 1))
            self.screen.fill((0, 0, 0))
            pygame.transform.scale(self._srcsurf, rect.size, self.screen.subsurface(rect))
        self._last_rect = rect
        return rect

    def set_presented_rect(self, rect) -> None:
        """Record a custom presenter's frame rect for mouse mapping."""
        self._last_rect = pygame.Rect(rect)

    # --- GPU compositing primitives -------------------------------------------------------------------
    # For viewers that build the window from a SMALL source frame with a free camera (zoom/pan) instead of
    # letterboxing the whole frame: upload the small frame ONCE, then let the GPU scale sub-rects of it to
    # window rects (srcrect->dstrect). This keeps present cost ~constant with window size — the CPU never
    # touches a window-sized buffer. Falls back to pygame.transform.scale per quad on the software path.
    def clear(self) -> None:
        """Clear the target to black. Call before a frame's draw_textured/fill_rect sequence."""
        if self.gpu:
            self.renderer.clear()
        else:
            self.screen.fill((0, 0, 0))

    def upload_frame(self, rgb, key: str = "main") -> tuple[int, int]:
        """Upload an H×W×3 uint8 frame as a named SOURCE (a streaming texture on GPU, a Surface on the
        software path). Draw regions of it with draw_textured(..., key). Several sources can coexist under
        different keys (e.g. a wide level + a small HUD). Only the source is uploaded, never a window-sized
        array. Returns (w, h) of the source."""
        arr = np.asarray(rgb, np.uint8)
        fh, fw = arr.shape[:2]
        if self._ksize.get(key) != (fw, fh):
            if self.gpu:
                self._ktex[key] = self._sdl2.Texture(self.renderer, (fw, fh), streaming=True)
            self._ksurf[key] = pygame.Surface((fw, fh))
            self._ksize[key] = (fw, fh)
        pygame.surfarray.blit_array(self._ksurf[key], arr.swapaxes(0, 1))
        if self.gpu:
            self._ktex[key].update(self._ksurf[key])
        return (fw, fh)

    def draw_textured(self, src_rect, dst_rect, key: str = "main") -> None:
        """GPU-scale a sub-rect of a named uploaded source (upload_frame) to a window rect. Nearest-neighbour
        (SDL's default scale quality) keeps pixel-art crisp when zoomed in."""
        sr = pygame.Rect(src_rect)
        dr = pygame.Rect(dst_rect)
        if self.gpu:
            tex = self._ktex.get(key)
            if tex is not None:
                tex.draw(srcrect=sr, dstrect=dr)
        else:
            surf = self._ksurf.get(key)
            if surf is not None:
                sr = sr.clip(surf.get_rect())
                if sr.w > 0 and sr.h > 0 and dr.w > 0 and dr.h > 0:
                    sub = surf.subsurface(sr)
                    pygame.transform.scale(sub, dr.size, self.screen.subsurface(
                        dr.clip(self.screen.get_rect())))

    def fill_rect(self, dst_rect, color) -> None:
        """Filled window-space rect (entity markers / overlays)."""
        dr = pygame.Rect(dst_rect)
        if self.gpu:
            self.renderer.draw_color = (int(color[0]), int(color[1]), int(color[2]), 255)
            self.renderer.fill_rect(dr)
            self.renderer.draw_color = (0, 0, 0, 255)
        else:
            self.screen.fill(color, dr.clip(self.screen.get_rect()))

    # --- input mapping --------------------------------------------------------------------------------
    def window_to_frame_norm(self, pos):
        """Window pixel -> normalized (u, v) within the GAME FRAME, clamped to it.

        A pointer-driven game needs the position in the frame's own space, and the frame is
        letterboxed: it does not fill the window whenever the window aspect differs from the
        frame's (any fullscreen/maximized window, and every phone screen).  Mapping against the
        window instead skews the cursor and offsets it by the bar size -- so this maps against
        the rect the last ``draw_game`` actually drew into.

        Returns None before the first frame is drawn (no rect to map against yet)."""
        r = self._last_rect
        if r is None:
            return None
        u = (pos[0] - r.x) / max(1, r.w - 1)
        v = (pos[1] - r.y) / max(1, r.h - 1)
        u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
        v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
        return round(u, 4), round(v, 4)

    def draw_overlay(self, surf, pos) -> None:
        """Composite a pygame Surface (fps readout / the F10 menu) on top at window pixel ``pos``, alpha-blended.
        A persistent streaming texture per size is re-uploaded each call (content changes every frame), so no
        per-frame GPU allocation."""
        if self.gpu:
            sz = surf.get_size()
            tex = self._ov.get(sz)
            if tex is None:
                if len(self._ov) > 6:
                    self._ov.clear()
                tex = self._sdl2.Texture(self.renderer, sz, streaming=True)
                tex.blend_mode = 1                                # SDL_BLENDMODE_BLEND (alpha)
                self._ov[sz] = tex
            tex.update(surf)
            tex.draw(dstrect=pygame.Rect(pos, sz))
        else:
            self.screen.blit(surf, pos)

    def new_overlay_canvas(self):
        """A transparent window-size surface to draw the modal F10 menu onto (then draw_overlay it)."""
        return pygame.Surface(self.get_size(), pygame.SRCALPHA)

    def flip(self) -> None:
        if self.opengl:
            pygame.display.flip()
        elif self.gpu:
            self.renderer.present()
        else:
            pygame.display.flip()

    # --- window state ---------------------------------------------------------------------------------
    def resize(self, w: int, h: int) -> None:
        """Handle a user window drag-resize (software path re-creates the surface; GPU auto-tracks)."""
        if self.opengl:
            size = (max(160, w), max(100, h))
            if self._gl_window is not None:
                self._gl_window.size = size
            else:
                self.screen = pygame.display.set_mode(size, self._opengl_flags)
        elif self.gpu:
            self.window.size = (max(160, w), max(100, h))
        else:
            self.screen = pygame.display.set_mode((max(160, w), max(100, h)), pygame.RESIZABLE)

    def toggle_fullscreen(self) -> bool:
        """Alt+Enter: flip borderless fullscreen, restoring the pre-fullscreen window size on the way back.

        Viewers call this on :func:`is_fullscreen_toggle`; returns the new state."""
        if not self.fullscreen:
            self._windowed_size = self.get_size()
        self.set_fullscreen(not self.fullscreen, windowed_size=self._windowed_size)
        self.fullscreen = not self.fullscreen
        return self.fullscreen

    def _set_borderless_desktop(self, window, on: bool, windowed_size) -> None:
        """Make an SDL window cover the desktop without any fullscreen flag."""
        if on:
            # Explicitly leave any inherited fullscreen state first. The
            # resulting window remains an ordinary SDL window throughout.
            window.set_windowed()
            self._windowed_position = tuple(window.position)
            self._windowed_borderless = bool(window.borderless)
            self._windowed_always_on_top = bool(window.always_on_top)
            self._windowed_resizable = bool(window.resizable)
            try:
                desktop_size = tuple(pygame.display.get_desktop_sizes()[0])
            except Exception:                                # noqa: BLE001
                info = pygame.display.Info()
                desktop_size = (info.current_w, info.current_h)
            window.borderless = True
            window.resizable = False
            window.position = (0, 0)
            window.size = desktop_size
            # A normal borderless window otherwise sits below the Windows
            # taskbar. Topmost gives it the expected fullscreen coverage while
            # Alt+Tab remains a normal window transition.
            window.always_on_top = True
            return

        window.always_on_top = self._windowed_always_on_top
        window.borderless = self._windowed_borderless
        window.resizable = self._windowed_resizable
        window.size = tuple(windowed_size or (1280, 800))
        if self._windowed_position is not None:
            window.position = self._windowed_position

    def set_fullscreen(self, on: bool, windowed_size=None) -> None:
        """Use a desktop-sized borderless window without an exclusive mode.

        SDL2-backed paths remain ordinary windows with their border disabled;
        the legacy surface fallback recreates an equivalent NOFRAME window.
        """
        if self.opengl:
            if self._gl_window is not None:
                self._set_borderless_desktop(
                    self._gl_window, on, windowed_size,
                )
            else:
                # Older pygame builds without Window.from_display_module still
                # get a borderless desktop window. NOFRAME is important:
                # pygame.FULLSCREEN here can select exclusive fullscreen.
                import os
                if on:
                    try:
                        dw, dh = pygame.display.get_desktop_sizes()[0]
                    except Exception:                        # noqa: BLE001
                        info = pygame.display.Info()
                        dw, dh = info.current_w, info.current_h
                    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
                    self.screen = pygame.display.set_mode(
                        (dw, dh), self._opengl_flags | pygame.NOFRAME,
                    )
                else:
                    os.environ.pop("SDL_VIDEO_WINDOW_POS", None)
                    self.screen = pygame.display.set_mode(
                        windowed_size or (1280, 800), self._opengl_flags,
                    )
            self._texsize = None
            return
        if self.gpu:
            self._set_borderless_desktop(self.window, on, windowed_size)
            self._ov.clear()                                      # window size changed -> stale overlay textures
        else:
            import os
            if on:
                try:
                    dw, dh = pygame.display.get_desktop_sizes()[0]
                except Exception:                                # noqa: BLE001
                    info = pygame.display.Info(); dw, dh = info.current_w, info.current_h
                os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
                self.screen = pygame.display.set_mode((dw, dh), pygame.NOFRAME)
            else:
                os.environ.pop("SDL_VIDEO_WINDOW_POS", None)
                self.screen = pygame.display.set_mode(windowed_size or (1280, 800), pygame.RESIZABLE)
        self._texsize = None                                     # force src-surface rebuild against the new target
