"""Watch the oracle run — a generic interactive viewer for any dos_re runtime.

This is the "show the game to a human" tool for bring-up and feedback: it
boots the original EXE (or resumes a snapshot), steps the VM in chunks,
decodes the framebuffer every host frame — VGA mode 13h linear, or the
320x200 16-colour EGA/VGA planar path via the shadow planes — presents it
through tools/display.py (GPU-accelerated, DOS 4:3 aspect by default), and
forwards your keyboard to the game (XT scancodes through KeyDispatcher ->
deliver_scancode, so quick taps survive per-frame polling).

It is deliberately game-agnostic and therefore approximate: no frame
boundaries, no hooks, no pel-pan/split-screen refinements — it shows whatever
the interpreted original draws, paced by --fps. The verifiers are the truth;
this is the window. A real adapter's interactive runner grows from this
template (see pre2_port/scripts/play.py for the full-featured worked example).

Usage:
    python tools/view.py --exe assets/GAME.EXE [--tail "..."] [--fps 60]
                         [--steps-per-frame 40000] [--timer-irqs-per-frame 0]
                         [--snapshot DIR] [--frames N] [--square-pixels]

--timer-irqs-per-frame delivers INT 08h that many times per host frame for
games that advance on the PIT ISR (leave 0 for retrace-paced games).
--frames N exits after N presented frames — headless smoke use with
SDL_VIDEODRIVER=dummy. Needs numpy + pygame (the framework core does not).

Origin: assembled from shipped parts (display.py presenter, render_frame.py's
decoders re-done in numpy, KeyDispatcher, deliver_scancode); the loop shape
follows the source repos' play.py runners.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from dos_re.cpu import HaltExecution  # noqa: E402
from dos_re.interrupts import deliver_interrupt, deliver_scancode  # noqa: E402
from dos_re.keyboard import KeyDispatcher  # noqa: E402
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE  # noqa: E402
from dos_re.runtime import create_runtime  # noqa: E402
from dos_re.snapshot import load_snapshot  # noqa: E402

WIDTH, HEIGHT = 320, 200
PLANAR_ROW_BYTES = 40


def _palette_array(dos) -> np.ndarray:
    pal = list(getattr(dos, "vga_palette", ()) or ())
    while len(pal) < 256:
        i = len(pal)
        pal.append((i, i, i))
    return np.asarray(pal[:256], dtype=np.uint8)


def decode_frame(rt) -> np.ndarray:
    """Return an HxWx3 uint8 array of the current screen."""
    mem = rt.cpu.mem
    pal = _palette_array(rt.dos)
    if mem.ega_planar or (rt.dos.video_mode & 0x7F) == 0x0D:
        start = mem.ega_display_start & 0xFFFF
        offs = (start + np.arange(HEIGHT)[:, None] * PLANAR_ROW_BYTES
                + np.arange(PLANAR_ROW_BYTES)[None, :]) & 0xFFFF
        idx = np.zeros((HEIGHT, PLANAR_ROW_BYTES, 8), dtype=np.uint8)
        for plane in range(4):
            base = EGA_APERTURE + plane * EGA_PLANE_STRIDE
            plane_bytes = np.frombuffer(mem.data, np.uint8, count=0x10000, offset=base)
            bits = np.unpackbits(plane_bytes[offs].reshape(HEIGHT, PLANAR_ROW_BYTES, 1), axis=2)
            idx |= bits << plane
        return pal[idx.reshape(HEIGHT, WIDTH)]
    # Linear VGA mode 13h (also the harmless default for anything else).
    arr = np.frombuffer(mem.data, np.uint8, count=WIDTH * HEIGHT, offset=0xA0000)
    return pal[arr.reshape(HEIGHT, WIDTH)]


# pygame key -> XT scan code (make). Break = make | 0x80.
def _scancode_table(pygame) -> dict[int, int]:
    k = pygame
    table = {
        k.K_ESCAPE: 0x01, k.K_MINUS: 0x0C, k.K_EQUALS: 0x0D, k.K_BACKSPACE: 0x0E,
        k.K_TAB: 0x0F, k.K_RETURN: 0x1C, k.K_LCTRL: 0x1D, k.K_RCTRL: 0x1D,
        k.K_LSHIFT: 0x2A, k.K_RSHIFT: 0x36, k.K_LALT: 0x38, k.K_RALT: 0x38,
        k.K_SPACE: 0x39, k.K_UP: 0x48, k.K_LEFT: 0x4B, k.K_RIGHT: 0x4D,
        k.K_DOWN: 0x50, k.K_COMMA: 0x33, k.K_PERIOD: 0x34, k.K_SLASH: 0x35,
        k.K_SEMICOLON: 0x27, k.K_QUOTE: 0x28, k.K_BACKQUOTE: 0x29,
        k.K_LEFTBRACKET: 0x1A, k.K_RIGHTBRACKET: 0x1B, k.K_BACKSLASH: 0x2B,
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exe", required=True, help="path to the original MZ executable")
    p.add_argument("--snapshot", default=None, help="resume from a snapshot directory")
    p.add_argument("--game-root", default=None)
    p.add_argument("--tail", default="", help="DOS command tail")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--steps-per-frame", type=int, default=40_000)
    p.add_argument("--timer-irqs-per-frame", type=int, default=0,
                   help="deliver INT 08h this many times per host frame")
    p.add_argument("--frames", type=int, default=0,
                   help="exit after N presented frames (0 = run until closed)")
    p.add_argument("--square-pixels", action="store_true",
                   help="par=1.0 instead of the DOS 4:3 look (par=1.2)")
    args = p.parse_args(argv)

    import pygame
    from display import Display

    if args.snapshot:
        rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    else:
        rt = create_runtime(args.exe, game_root=args.game_root,
                            command_tail=args.tail.encode("ascii"))
    rt.cpu.trace_enabled = False

    pygame.init()
    display = Display((WIDTH * 3, int(HEIGHT * 1.2) * 3),
                      title=f"dos_re oracle — {Path(args.exe).name}")
    display.par = 1.0 if args.square_pixels else 1.2
    dispatcher = KeyDispatcher(lambda sc: deliver_scancode(rt, sc))
    scancodes = _scancode_table(pygame)
    clock = pygame.time.Clock()

    presented = 0
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.VIDEORESIZE:
                display.resize(event.w, event.h)
            elif event.type in (pygame.KEYDOWN, pygame.KEYUP):
                sc = scancodes.get(event.key)
                if sc is not None:
                    (dispatcher.post_down if event.type == pygame.KEYDOWN
                     else dispatcher.post_up)(sc)
        dispatcher.pump()

        for _ in range(max(0, args.timer_irqs_per_frame)):
            deliver_interrupt(rt, 0x08)
        try:
            rt.cpu.run(args.steps_per_frame)
        except HaltExecution:
            pass
        if rt.cpu.halted:
            print(f"program halted at {rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} "
                  f"after {rt.cpu.instruction_count} instructions")
            running = False

        display.draw_game(decode_frame(rt))
        display.flip()
        presented += 1
        if args.frames and presented >= args.frames:
            running = False
        clock.tick(args.fps)

    print(f"presented {presented} frames; CPU at "
          f"{rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X}, {rt.cpu.instruction_count} instructions")
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
