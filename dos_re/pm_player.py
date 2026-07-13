"""PM (DOS/4GW) play runner: live viewer + headless runs for CPU386 runtimes.

The protected-mode counterpart of :mod:`dos_re.player`, reduced to what the
PM runtime supports today: a pygame window presenting the VGA screen
(chained 13h or unchained Mode X via :func:`~dos_re.dos4gw.render_pm_frame`),
keyboard delivered as set-1 scancodes through the emulated 8042 KBC
(extended-key E0 pairs included), mouse through the INT 33h driver state,
pacing by wall-clock vsync (``dos.time_source`` drives the program's own
3DAh retrace waits at ~70 Hz real time), F10 screenshot / F12 snapshot, F11
demo record, ``--play-demo`` deterministic replay, and ``--snapshot`` resume.

Demos are keyed to the game's own frame counter (an adapter-supplied
``frame_tick_addr`` hit once per frame — see ``pm_input_demo``), so a demo
recorded live replays identically headless: the perfect way to hand a
captured game state back for oracle-verified recovery.

FRONTEND RING module: pygame imports stay lazy so headless use (and
``import dos_re``) never require it.

A game port's ``scripts/play.py`` is a thin wrapper::

    from dos_re.pm_player import main
    raise SystemExit(main(argv, default_exe="assets/GAME.EXE",
                          create_runtime=my_create_runtime,
                          title="My Game", boot_keys=(0x20,)))

Origin: promoted from the Krypton Egg port's play runner (the first DOS/4GW
title), generalized to any PM runtime.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .dos4gw import DosInputExhausted, render_pm_frame
from .frame_verify import write_rgb_png

# pygame key name -> set-1 scancode (make code; break = make | 0x80).
# Extended keys (arrows...) are (0xE0, code) tuples.
SCANCODES = {
    "escape": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B, "-": 0x0C,
    "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B,
    "return": 0x1C, "left ctrl": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28, "`": 0x29,
    "left shift": 0x2A, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31,
    "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35, "right shift": 0x36,
    "left alt": 0x38, "space": 0x39, "caps lock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F,
    "f6": 0x40, "f7": 0x41, "f8": 0x42, "f9": 0x43,
    "up": (0xE0, 0x48), "down": (0xE0, 0x50),
    "left": (0xE0, 0x4B), "right": (0xE0, 0x4D),
}


def send_key(dos, name: str, make: bool) -> None:
    """Deliver one pygame-named key as KBC scancodes (make or break)."""
    sc = SCANCODES.get(name)
    if sc is None:
        return
    if isinstance(sc, tuple):
        dos.press_scancode(sc[0])
        dos.press_scancode(sc[1] | (0x00 if make else 0x80))
    else:
        dos.press_scancode(sc | (0x00 if make else 0x80))


class _PcmSink:
    """Minimal Sound Blaster PCM consumer: drains ``sb.pcm_out`` (8-bit
    unsigned mono at the DSP-programmed rate) into a pygame mixer channel.
    Lazy: the mixer initializes at the first chunk, at the stream's rate."""

    def __init__(self, sb):
        self.sb = sb
        self.channel = None

    def pump(self):
        import pygame
        sb = self.sb
        if sb is None or not sb.pcm_out or not sb.sample_rate:
            return
        data = bytes(sb.pcm_out)
        del sb.pcm_out[:]
        if self.channel is None:
            pygame.mixer.quit()
            pygame.mixer.init(frequency=sb.sample_rate, size=8, channels=1, buffer=512)
            self.channel = pygame.mixer.Channel(0)
        snd = pygame.mixer.Sound(buffer=data)
        if self.channel.get_busy():
            self.channel.queue(snd)
        else:
            self.channel.play(snd)


def run_viewer(rt, *, scale: int = 3, title: str = "dos_re PM",
               artifacts_dir: str | Path = "artifacts",
               frame_tick_addr: int | None = None,
               record_demo: str | None = None) -> int:
    import pygame
    from .pm_input_demo import PMInputDemo, FrameClock

    pygame.init()
    # A fixed 4:3 canvas: every VGA geometry this runtime produces (320x200
    # mode 13h, Mode X 320x240 / 320x400) displays at the aspect a real
    # monitor showed — the frame is scaled to the canvas each present.
    win = pygame.display.set_mode((320 * scale, 240 * scale))
    pygame.display.set_caption(title)
    pygame.mouse.set_visible(False)      # the game draws its own cursor

    dos, cpu = rt.dos, rt.cpu
    dos.time_source = time.monotonic     # 3DAh retrace advances at 70 Hz real time
    pcm = _PcmSink(dos.sound_blaster)
    artifacts = Path(artifacts_dir)

    # Demo recording (F11): a FrameClock keyed to the game's per-frame entry
    # tags input with the frame index, so the demo replays deterministically.
    mouse_norm = [0.5, 0.5]
    rec = {"demo": None, "start": 0, "last_mouse": None}

    def on_frame(frame):
        if rec["demo"] is not None:
            f = frame - rec["start"]
            sample = [round(mouse_norm[0], 4), round(mouse_norm[1], 4),
                      getattr(dos, "mouse_buttons", 0)]
            if sample != rec["last_mouse"]:
                rec["demo"].add(f, "mouse", sample)
                rec["last_mouse"] = sample

    clock = FrameClock(cpu, frame_tick_addr, on_frame) if frame_tick_addr else None

    def record_key(make, name):
        if rec["demo"] is not None and clock is not None:
            rec["demo"].add(clock.frame - rec["start"], "key", [make, name])

    # Pacing.  With an adapter frame clock we run the game with DETERMINISTIC
    # retrace (the vsync wait exits in ~2 reads instead of spinning thousands
    # of emulated 3DAh reads per frame) and throttle to 70 logical frames/sec
    # by wall-clock — so the CPU emulates one frame's real work per frame, not
    # the busy-wait.  Without a frame clock, fall back to wall-clock retrace.
    paced = clock is not None
    if paced:
        dos.time_source = None
    period = 1 / 70.0
    start = time.monotonic()
    next_present = start
    frame_budget = 4_000            # small chunks so we stop near a frame boundary

    def advance():
        """Run the game forward to the frame due by wall-clock (paced) or one
        present's worth (unpaced)."""
        nonlocal waiting_console, running
        if waiting_console:
            return
        try:
            if paced:
                due = int((time.monotonic() - start) / period)
                guard = 0
                while clock.frame < due and guard < 600 and not cpu.halted:
                    cpu.run(frame_budget)
                    guard += 1
            else:
                cpu.run(20_000)
        except DosInputExhausted:
            waiting_console = True
        except Exception as e:  # noqa: BLE001 — the fail-loud frontier
            print(f"STOP at eip=0x{cpu.eip:X}: {type(e).__name__}: {e}")
            running = False
        if cpu.halted:
            print(f"program exited (code {dos.exit_code})")
            running = False

    waiting_console = False
    running = True
    while running:
        advance()

        now = time.monotonic()
        if now < next_present:
            time.sleep(min(next_present - now, 0.005))
            continue
        next_present = max(next_present + period, now)
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type in (pygame.KEYDOWN, pygame.KEYUP):
                make = ev.type == pygame.KEYDOWN
                name = pygame.key.name(ev.key)
                if make and name == "f10":
                    shots = artifacts / "screenshots"
                    shots.mkdir(parents=True, exist_ok=True)
                    rgb, w, h = render_pm_frame(dos)
                    out = shots / f"shot_{int(now * 1000)}.png"
                    write_rgb_png(out, rgb, width=w, height=h)
                    print(f"screenshot -> {out}")
                    continue
                if make and name == "f12":
                    from .pm_snapshot import save_pm_snapshot
                    out = artifacts / "snapshots" / f"snap_{int(now * 1000)}"
                    save_pm_snapshot(rt, out)
                    print(f"snapshot -> {out}")
                    continue
                if make and name == "f11":
                    if clock is None:
                        print("demo recording needs a frame_tick_addr (adapter)")
                    elif rec["demo"] is None:
                        rec["demo"] = PMInputDemo(frame_tick_addr)
                        rec["start"] = clock.frame
                        rec["last_mouse"] = None
                        print("demo recording STARTED (F11 again to stop)")
                    else:
                        demo = rec["demo"]
                        demo.total_frames = clock.frame - rec["start"]
                        path = record_demo or (artifacts / "demos" /
                                               f"demo_{int(now * 1000)}.json")
                        demo.save(path)
                        rec["demo"] = None
                        print(f"demo recording STOPPED -> {path} "
                              f"({demo.total_frames} frames, {len(demo.events)} events)")
                    continue
                record_key(make, name)
                send_key(dos, name, make)
                if make and waiting_console:
                    ch = ev.unicode
                    if ch:
                        dos.key_queue.append(ord(ch[0]) & 0xFF)
                        waiting_console = False
            elif ev.type == pygame.MOUSEMOTION:
                mx, my = ev.pos
                ww, wh = win.get_size()
                # Window-relative position, mapped onto the game's own
                # INT 33h virtual range by the host.
                mouse_norm[0] = mx / max(1, ww - 1)
                mouse_norm[1] = my / max(1, wh - 1)
                dos.set_mouse_norm(mouse_norm[0], mouse_norm[1])
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                down = ev.type == pygame.MOUSEBUTTONDOWN
                bit = {1: 1, 3: 2}.get(ev.button, 0)
                cur = getattr(dos, "mouse_buttons", 0)
                dos.mouse_buttons = (cur | bit) if down else (cur & ~bit)

        rgb, w, h = render_pm_frame(dos)
        frame = pygame.image.frombuffer(rgb, (w, h), "RGB").convert(win)
        pygame.transform.scale(frame, win.get_size(), win)
        pygame.display.flip()
        pcm.pump()
    pygame.quit()
    return 0


def run_replay(rt, demo_path, *, boot_keys=(), extra_frames: int = 30,
               max_steps: int = 200_000_000, snapshot_dir: str | None = None,
               png: str = "", show: bool = False, scale: int = 3,
               title: str = "dos_re PM (replay)") -> int:
    """Replay an input demo deterministically (no wall-clock pacing).

    Re-injects each frame's recorded input at the game's own frame boundary,
    then runs ``extra_frames`` past the demo's end.  Optionally saves a
    snapshot / PNG of the resulting state, or shows it in a window."""
    from .pm_input_demo import PMInputDemo, FrameClock

    demo = PMInputDemo.load(demo_path)
    if demo.frame_tick_addr is None:
        print("demo has no frame_tick_addr; cannot replay")
        return 1
    for k in boot_keys:
        rt.dos.key_queue.append(k)
    dos = rt.dos
    by_frame = demo.by_frame()
    end_frame = demo.total_frames + extra_frames
    done = {"flag": False}

    win = surf = None
    if show:
        import pygame
        pygame.init()
        win = pygame.display.set_mode((320 * scale, 240 * scale))
        pygame.display.set_caption(title)

    def on_frame(frame):
        for kind, payload in by_frame.get(frame, ()):
            if kind == "key":
                send_key(dos, payload[1], payload[0])
            elif kind == "mouse":
                dos.set_mouse_norm(payload[0], payload[1])
                dos.mouse_buttons = payload[2]
        if frame >= end_frame:
            done["flag"] = True

    FrameClock(rt.cpu, demo.frame_tick_addr, on_frame)
    steps = 0
    try:
        while not done["flag"] and steps < max_steps and not rt.cpu.halted:
            rt.cpu.run(50_000)
            steps += 50_000
    except Exception as e:  # noqa: BLE001
        print(f"STOP at eip=0x{rt.cpu.eip:X}: {type(e).__name__}: {e}")
    print(f"replayed to frame ~{demo.total_frames}+{extra_frames}; "
          f"{rt.cpu.instruction_count} instructions")
    if snapshot_dir:
        from .pm_snapshot import save_pm_snapshot
        save_pm_snapshot(rt, snapshot_dir)
        print(f"snapshot -> {snapshot_dir}")
    if png:
        rgb, w, h = render_pm_frame(dos)
        write_rgb_png(Path(png), rgb, width=w, height=h)
        print(f"wrote {png}")
    if show:
        import pygame
        rgb, w, h = render_pm_frame(dos)
        img = pygame.image.frombuffer(rgb, (w, h), "RGB").convert(win)
        pygame.transform.scale(img, win.get_size(), win)
        pygame.display.flip()
        waiting = True
        while waiting:
            for ev in pygame.event.get():
                if ev.type in (pygame.QUIT, pygame.KEYDOWN):
                    waiting = False
        pygame.quit()
    return 0


def run_headless(rt, *, steps: int, png: str = "", boot_keys=()) -> int:
    for k in boot_keys:
        rt.dos.key_queue.append(k)
    try:
        rt.cpu.run(steps)
    except Exception as e:  # noqa: BLE001
        print(f"STOP after {rt.cpu.instruction_count} at eip=0x{rt.cpu.eip:X}: "
              f"{type(e).__name__}: {e}")
        return 1
    print(f"ran {rt.cpu.instruction_count} instructions; halted={rt.cpu.halted}")
    if png:
        rgb, w, h = render_pm_frame(rt.dos)
        write_rgb_png(Path(png), rgb, width=w, height=h)
        print(f"wrote {png}")
    return 0


def main(argv=None, *, default_exe: str | None = None, create_runtime=None,
         title: str = "dos_re PM", boot_keys=(), description: str | None = None,
         artifacts_dir: str | Path = "artifacts",
         sound_blaster: tuple[int, int, int] | None = None,
         frame_tick_addr: int | None = None) -> int:
    """The standard PM play-runner CLI.  Game wrappers supply the defaults.

    ``frame_tick_addr`` (an address the program executes once per frame)
    enables F11 demo recording and ``--play-demo`` deterministic replay."""
    from .pm_snapshot import load_pm_snapshot
    from .runtime import create_pm_runtime

    ap = argparse.ArgumentParser(description=description or main.__doc__)
    ap.add_argument("--exe", default=default_exe, required=default_exe is None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--steps", type=int, default=20_000_000,
                    help="instruction budget (headless)")
    ap.add_argument("--png", default="", help="render the final screen to this PNG")
    ap.add_argument("--scale", type=int, default=3, help="window scale factor")
    ap.add_argument("--snapshot", default="", help="resume from a saved snapshot dir")
    ap.add_argument("--no-sound", action="store_true",
                    help="do not attach the emulated Sound Blaster")
    ap.add_argument("--record-demo", default="",
                    help="viewer: default path for an F11 recording")
    ap.add_argument("--play-demo", default="",
                    help="replay an input demo deterministically (headless unless --show)")
    ap.add_argument("--save-snapshot", default="",
                    help="replay: save a snapshot of the final state here")
    ap.add_argument("--show", action="store_true",
                    help="replay: show the final frame in a window")
    args = ap.parse_args(argv)

    build = create_runtime or create_pm_runtime
    headless_clock = args.headless or (args.play_demo and not args.show)
    if args.snapshot:
        rt = load_pm_snapshot(args.exe, args.snapshot)
    else:
        rt = build(args.exe)
    if sound_blaster is not None and not args.no_sound:
        base, irq, dma = sound_blaster
        rt.dos.attach_sound_blaster(base=base, irq=irq, dma=dma,
                                    clock=None if headless_clock else time.monotonic,
                                    anchor_cadence=not headless_clock)
    if args.play_demo:
        return run_replay(rt, args.play_demo, boot_keys=boot_keys,
                          snapshot_dir=args.save_snapshot or None, png=args.png,
                          show=args.show, scale=args.scale, title=title)
    if args.headless:
        return run_headless(rt, steps=args.steps, png=args.png, boot_keys=boot_keys)
    return run_viewer(rt, scale=args.scale, title=title, artifacts_dir=artifacts_dir,
                      frame_tick_addr=frame_tick_addr,
                      record_demo=args.record_demo or None)
