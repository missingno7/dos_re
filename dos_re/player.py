"""dos_re.player — the game-agnostic core every port's ``scripts/play.py`` builds on.

``play.py`` is the human entry point of a game port: run the (hybrid) game in a live
viewer, resume/save snapshots, record/replay input demos, take screenshots — the
artifacts a human hands to the AI during reverse engineering.  Every port needs the
same skeleton; before this module each port copy-pasted and mutated it (four CLIs,
four flag vocabularies).  This module owns the game-agnostic 90%:

  * the STANDARD CLI (same flag names in every port — see ``build_arg_parser``):
      run mode      viewer by default; ``--headless`` disables it
      snapshots     ``--snapshot DIR`` resume, ``--save-snapshot [DIR]`` on exit
      demos         ``--record-demo NAME``, ``--play-demo DIR`` (+ ``--demo-continue``
                    to hand the game to the player when the demo ends), ``--demo-dir``
      hook modes    ``--no-replacements``, ``--safe-hooks``, ``--verify-hooks``,
                    ``--trace-hooks`` (defined for every port from day one; a port
                    without that tier fails LOUD, it never silently ignores the flag)
      pacing        ``--present-hz``, ``--steps-per-frame``, ``--timer-irqs-per-frame``,
                    ``--frames N`` / ``--steps N`` budgets (headless smokes)
      presentation  ``--scale``, ``--square-pixels``
      boot          ``--exe``, ``--game-root``, ``--dos-args``
  * the viewer loop: pygame window (``dos_re.display.Display``), keyboard forwarding
    as XT scancodes (``KeyDispatcher`` -> ``deliver_scancode``), and the standard
    hotkeys — F10 screenshot, F11 demo-record toggle, F12 snapshot;
  * headless demo replay (fast, deterministic, no pygame);
  * crash handling: a gap snapshot is written on any unhandled VM exception;
  * the standard exit report (status / frames / steps / CPU state).

A port subclasses :class:`GameFrontend` and overrides only what its game needs —
usually ``create_runtime``/``load_snapshot_runtime`` (its own adapter boot),
``advance_frame`` (its pacing policy) and the pacing defaults.  The DEFAULT model is
the simple deterministic one proven by skyroads_port: a fixed instruction budget per
frame with N timer IRQs, so the frame index alone is the demo clock and record/replay
are trivially deterministic — **within one hook mode**: a step-budget clock is
mode-DEPENDENT (a hook is one step() however much ASM it replaces), so demos
recorded under it only replay under the same installed-hook set (see
docs/demos_and_snapshots.md, "the boundary-clock invariant").  For hook-mode-
independent demos, override ``advance_frame`` with a GAME-PROGRESS clock — run
until the game's own frame boundary (its present/page-flip, a boundary address
crossing à la pm_input_demo's ``frame_tick_addr``, or a registered input-wait) —
so the frame index counts game frames, not interpreter steps.  A mature port
(pre2_port) replaces ``advance_frame`` with its own wall-clock/PIT model and
keeps the CLI contract.

Import discipline: this module is the FRONTEND RING (see tools/lint.py).  It keeps
numpy/pygame imports lazy — importing ``dos_re.player`` (and running headless demo
replay) needs neither installed; only opening the viewer or taking a screenshot does.

Worked examples: the Lemmings pilot's runners (lemmings_port/scripts/) and the tools/new_project.py starter.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dos_re.cpu import HaltExecution, UnsupportedInstruction
from dos_re.dos import ConsoleInputWouldBlock
from dos_re.hooks import registry as hook_registry
from dos_re.input_demo import InputDemoPlayback, InputDemoRecorder, mouse_sample
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.keyboard import KeyDispatcher
# NOTE: the EXE loader (create_runtime) and the EXE-based load_snapshot are
# imported LAZILY inside the default GameFrontend methods below, not at module
# level.  A frontend that overrides create_runtime/load_snapshot_runtime (e.g.
# the strict-VMless runner) then never pulls the loader onto its import graph
# (scripts/lint_vmless_independence.py).  write_snapshot is EXE-free.
from dos_re.snapshot import write_snapshot

WIDTH, HEIGHT = 320, 200
PLANAR_ROW_BYTES = 40


# --- default framebuffer decode (text modes + VGA mode 13h linear + EGA/VGA 16-colour planar) ----

def decode_frame_default(rt):
    """Return an HxWx3 uint8 array of the current screen (numpy imported lazily).

    Game-agnostic and therefore approximate: no pel-pan/split-screen refinements —
    it shows whatever the interpreted original draws.  BIOS text modes (0-3, 7)
    render through :mod:`dos_re.textmode` (so DOS-era text boot menus/setup
    screens are visible out of the box); ports with fancier video (CGA/Tandy)
    override ``GameFrontend.decode_frame``.
    """
    import numpy as np

    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
    from dos_re.textmode import decode_text_frame, is_text_display

    if is_text_display(rt.dos):
        return decode_text_frame(rt)

    mem = rt.cpu.mem
    pal = list(getattr(rt.dos, "vga_palette", ()) or ())
    while len(pal) < 256:
        i = len(pal)
        pal.append((i, i, i))
    pal = np.asarray(pal[:256], dtype=np.uint8)
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


def _timestamp_dir(root: Path, prefix: str) -> Path:
    return Path(root) / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"


class HookModeUnsupported(SystemExit):
    """A hook-mode flag was passed that this port has no implementation for.

    The flags exist in every port's CLI from day one so the vocabulary is stable;
    a port that has not built that tier yet fails LOUD instead of silently running
    something else (the no-silent-fallbacks rule applies to the CLI too).
    """

    def __init__(self, flag: str) -> None:
        super().__init__(f"{flag} is not implemented by this port yet "
                         f"(see dos_re.player.GameFrontend.apply_hook_mode)")


class GameFrontend:
    """Per-game adapter for the standard play runner.  Subclass and override.

    The defaults implement the SIMPLE DETERMINISTIC model: fixed
    ``--steps-per-frame`` instruction budget + ``--timer-irqs-per-frame`` INT 08h
    ticks per frame, no wall-clock time source — the frame index IS the demo clock.
    Start every new port on this model; replace ``advance_frame`` only when the
    game's own timing demands it (and then also extend ``demo_metadata`` /
    ``apply_demo_metadata`` so replays restore your knobs).
    """

    #: used in window titles, demo metadata and artifact filename prefixes
    name = "game"

    # --- CLI defaults (surface them as class attrs so subclasses just assign) ---
    default_exe: str | None = None            # None -> --exe is required
    default_game_root: str | None = None
    default_dos_args = ""
    default_steps_per_frame = 40_000
    default_timer_irqs_per_frame = 0
    default_present_hz = 60
    default_scale = 3
    #: "adlib" turns on the observer-only OPL3 + PC-speaker sink in the viewer
    default_audio = "off"

    def __init__(self, root: Path | str) -> None:
        #: the PORT repo root; artifacts (snapshots/demos/screenshots) live under it
        self.root = Path(root)
        self.artifacts_dir = self.root / "artifacts"

    # --- CLI ------------------------------------------------------------------

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Add game-specific flags.  Never rename or repurpose the standard set."""

    # --- runtime construction ---------------------------------------------------

    def create_runtime(self, args: argparse.Namespace):
        """Boot a fresh runtime.  Ports override to call their own adapter's
        ``create_<game>_runtime`` (which installs their hooks)."""
        if not args.exe:
            raise SystemExit("--exe is required (this frontend has no default_exe)")
        from dos_re.runtime import create_runtime  # lazy: keeps the loader off
        return create_runtime(args.exe, game_root=args.game_root,  # an override's graph
                              command_tail=args.dos_args)

    def load_snapshot_runtime(self, args: argparse.Namespace, snapshot_dir: str | Path):
        """Resume from a snapshot directory."""
        from dos_re.snapshot import load_snapshot  # lazy (see create_runtime)
        return load_snapshot(args.exe, snapshot_dir, game_root=args.game_root)

    # --- per-frame behaviour ------------------------------------------------------

    def advance_frame(self, rt, args: argparse.Namespace, frame: int) -> None:
        """Advance the VM one displayed/simulated frame.  THE pacing extension point."""
        for _ in range(max(0, args.timer_irqs_per_frame)):
            deliver_interrupt(rt, 0x08)
        rt.cpu.run(args.steps_per_frame)

    def decode_frame(self, rt):
        """Return the current screen as an HxWx3 uint8 array."""
        return decode_frame_default(rt)

    def deliver_input(self, rt, scancode: int) -> None:
        """Deliver one XT scancode to the game (override e.g. to bound ISR steps)."""
        deliver_scancode(rt, scancode)

    # --- demo determinism ---------------------------------------------------------

    def demo_metadata(self, args: argparse.Namespace) -> dict[str, object]:
        """Reproducibility knobs a replay must match to stay deterministic."""
        return {
            "game": self.name,
            "exe": Path(args.exe).name if args.exe else "",
            "command_tail": args.dos_args,
            "steps_per_frame": int(args.steps_per_frame),
            "timer_irqs_per_frame": int(args.timer_irqs_per_frame),
        }

    def apply_demo_metadata(self, args: argparse.Namespace, meta: dict) -> None:
        """Restore the recorded pacing knobs before a replay."""
        if "steps_per_frame" in meta:
            args.steps_per_frame = int(meta["steps_per_frame"])
        if "timer_irqs_per_frame" in meta:
            args.timer_irqs_per_frame = int(meta["timer_irqs_per_frame"])

    # --- hook modes -----------------------------------------------------------------

    def apply_hook_mode(self, rt, args: argparse.Namespace) -> None:
        """Apply --no-replacements / --safe-hooks / --verify-hooks / --trace-hooks.

        The generic base handles ``--no-replacements`` (uninstall every registered
        replacement hook, keeping framework-level hooks like the BIOS INT9 ISR) and
        fails loud on the tiers it cannot provide.  Ports with hook tiers override.
        """
        if args.no_replacements:
            for key in hook_registry.replacements:
                rt.cpu.replacement_hooks.pop(key, None)
                rt.cpu.hook_names.pop(key, None)
        if args.safe_hooks:
            raise HookModeUnsupported("--safe-hooks")
        if args.verify_hooks:
            raise HookModeUnsupported("--verify-hooks")
        if args.trace_hooks:
            raise HookModeUnsupported("--trace-hooks")

    # --- presentation ------------------------------------------------------------

    def window_title(self, args: argparse.Namespace, mode: str) -> str:
        exe = Path(args.exe).name if args.exe else self.name
        return f"{exe} -- dos_re VM ({mode})"

    def create_audio_sink(self, pygame, rt, args: argparse.Namespace):
        """Viewer audio, or None.  The default honours ``--audio adlib`` with the
        observer-only OPL3 + PC-speaker sink (never affects game state; demos
        replay identically with audio on or off).  Ports with another audio
        architecture (e.g. digital SB-DMA) override this; the returned object
        just needs a ``pump()`` method called once per presented frame."""
        if args.audio != "adlib":
            return None
        from dos_re.audio_sink import AdlibSpeakerSink

        sink = AdlibSpeakerSink(pygame, rt, args.present_hz)
        return sink if sink.available else None


# --- CLI ------------------------------------------------------------------------------------------

def build_arg_parser(frontend: GameFrontend,
                     description: str | None = None) -> argparse.ArgumentParser:
    """The STANDARD play.py CLI.  Ports add flags via ``frontend.add_arguments``."""
    p = argparse.ArgumentParser(description=description,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    boot = p.add_argument_group("boot")
    boot.add_argument("--exe", default=frontend.default_exe,
                      help="path to the original MZ executable")
    boot.add_argument("--game-root", default=frontend.default_game_root,
                      help="directory containing the game's data files")
    boot.add_argument("--dos-args", default=frontend.default_dos_args,
                      help="raw DOS command tail to pass to the executable")

    mode = p.add_argument_group("run mode")
    mode.add_argument("--headless", action="store_true",
                      help="skip the live pygame viewer (default: viewer on)")
    mode.add_argument("--frames", type=int, default=0,
                      help="exit after N frames (0 = run until closed; headless smokes)")
    mode.add_argument("--steps", type=int, default=None,
                      help="max VM instructions to execute (headless default: 1,000,000)")

    snap = p.add_argument_group("snapshots")
    snap.add_argument("--snapshot", help="continue from an existing snapshot directory")
    snap.add_argument("--save-snapshot", nargs="?", const="auto",
                      help="save a VM snapshot on exit; optional directory path")

    demo = p.add_argument_group("demos")
    demo.add_argument("--record-demo", metavar="NAME",
                      help="(viewer) start recording an input demo immediately "
                           "(F11 toggles at any time)")
    demo.add_argument("--play-demo", metavar="DIR",
                      help="replay a recorded demo dir (viewer unless --headless)")
    demo.add_argument("--demo-continue", action="store_true",
                      help="(with --play-demo) when the demo ends, hand the game over "
                           "to live player input instead of stopping")
    demo.add_argument("--demo-dir", default=str(frontend.artifacts_dir / "demos"),
                      help="directory to write recorded demos into")

    hooks = p.add_argument_group("hook modes")
    hooks.add_argument("--no-replacements", action="store_true",
                       help="ORACLE mode: pure original ASM, no recovered hooks")
    hooks.add_argument("--safe-hooks", action="store_true",
                       help="original game logic with only the render/decode-owned "
                            "hook tier (fails loud if this port has no such tier)")
    hooks.add_argument("--verify-hooks", action="store_true",
                       help="run the ASM oracle and diff each recovered replacement "
                            "against it (fails loud if this port has no verifier)")
    hooks.add_argument("--trace-hooks", action="store_true",
                       help="hybrid runtime + a live tally of which hooks fire "
                            "(fails loud if this port has no tracer)")

    pace = p.add_argument_group("pacing")
    pace.add_argument("--present-hz", type=int, default=frontend.default_present_hz,
                      help="viewer presents per second")
    pace.add_argument("--steps-per-frame", type=int,
                      default=frontend.default_steps_per_frame,
                      help="VM instructions per displayed/simulated frame")
    pace.add_argument("--timer-irqs-per-frame", type=int,
                      default=frontend.default_timer_irqs_per_frame,
                      help="INT 08h timer ticks delivered per frame (games that idle "
                           "on the PIT ISR hang forever without this)")

    view = p.add_argument_group("presentation")
    view.add_argument("--scale", type=int, default=frontend.default_scale,
                      help="initial viewer window scale")
    view.add_argument("--square-pixels", action="store_true",
                      help="par=1.0 instead of the DOS 4:3 look (par=1.2)")
    view.add_argument("--audio", default=frontend.default_audio,
                      choices=("adlib", "off"),
                      help="viewer audio: 'adlib' = observer-only OPL3 + PC-speaker "
                           "sink (never affects game state); 'off'")

    frontend.add_arguments(p)
    return p


# --- shared run-loop plumbing ---------------------------------------------------------------------

def _save_exit_snapshot(frontend: GameFrontend, rt, args, *, status: str) -> None:
    if not args.save_snapshot:
        return
    out = (_timestamp_dir(frontend.artifacts_dir, f"snapshot_{frontend.name}")
           if args.save_snapshot == "auto" else Path(args.save_snapshot))
    write_snapshot(rt, out, status=status, steps=rt.cpu.instruction_count, trace_tail=())
    print(f"snapshot: {out}")


def _save_gap_snapshot(frontend: GameFrontend, rt, *, status: str) -> None:
    """Any unhandled VM exception leaves a resumable snapshot for diagnosis."""
    try:
        out = _timestamp_dir(frontend.artifacts_dir, f"gap_snapshot_{frontend.name}")
        write_snapshot(rt, out, status=status, steps=rt.cpu.instruction_count, trace_tail=())
        print(f"gap snapshot saved: {out}")
    except Exception as save_exc:  # noqa: BLE001
        print(f"(could not save gap snapshot: {save_exc})")


def _diagnostic_lines(rt) -> list[str]:
    """Game-agnostic context printed on any halt/crash, cheap enough to
    always compute (no per-instruction tracing overhead): DOS console output
    (many DOS programs print a plain-text reason — "Not enough memory",
    "Cannot find X" — before exiting, which otherwise vanishes silently), a
    compact DOS memory-allocator summary, and open file handles (useful when
    the failure is mid asset-load). "program halted" alone hides all of this."""
    lines = []
    dos = getattr(rt, "dos", None)
    if dos is None:
        return lines
    stdout = "".join(getattr(dos, "stdout", [])).strip()
    if stdout:
        lines.append(f"dos stdout: {stdout!r}")
    allocs = getattr(dos, "allocations", None)
    if allocs:
        total = sum(allocs.values()) * 16
        lines.append(f"dos memory: {len(allocs)} live allocations, {total:,} bytes; "
                     f"next_alloc_segment={dos.next_alloc_segment:04X} "
                     f"limit={dos.allocation_limit_segment:04X}")
    files = getattr(dos, "files", None)
    if files:
        names = ", ".join(f"{h}:{fh.path.name}@{fh.pos}/{len(fh.data)}" for h, fh in sorted(files.items()))
        lines.append(f"open files: {names}")
    return lines


def _exit_report(rt, *, status: str, frames: int) -> int:
    print(f"status: {status}")
    for line in _diagnostic_lines(rt):
        print(f"  {line}")
    print(f"frames: {frames}  steps: {rt.cpu.instruction_count:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    return 0 if not status.startswith(("unsupported", "exception")) else 1


def _step_frame(frontend: GameFrontend, rt, args, frame: int) -> tuple[str | None, bool]:
    """One guarded frame advance.  Returns (status_or_None, keep_running).

    Every failure mode prints the cheap diagnostics (_diagnostic_lines) and
    saves a resumable gap snapshot — not just unhandled exceptions. A bare
    "program halted"/"unsupported instruction" with no further context meant
    the only way to diagnose it was to reproduce it by hand from scratch."""
    try:
        frontend.advance_frame(rt, args, frame)
    except ConsoleInputWouldBlock:
        return "waiting for DOS key", True
    except HaltExecution:
        status = "program halted"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    except UnsupportedInstruction as exc:
        status = f"unsupported instruction: {exc}"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    except Exception as exc:  # noqa: BLE001 — keep bring-up useful
        import traceback
        traceback.print_exc()
        status = f"exception: {type(exc).__name__}: {exc}"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    return None, True


# --- headless -----------------------------------------------------------------------------------

def run_replay_headless(frontend: GameFrontend, rt, args,
                        playback: InputDemoPlayback) -> int:
    """Fast deterministic demo replay: no pygame, no pacing, no presentation."""
    frame = 0
    status = "demo replay complete"
    while not playback.finished(frame):
        if args.frames and frame >= args.frames:
            status = f"frame budget reached ({args.frames})"
            break
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: frontend.deliver_input(r, sc))
        new_status, keep_running = _step_frame(frontend, rt, args, frame)
        if new_status:
            status = new_status
        if not keep_running:
            break
        frame += 1
    print(f"events_applied={playback.next_event_index}/{len(playback.events)}")
    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame)


def run_headless(frontend: GameFrontend, rt, args) -> int:
    """Bounded headless run (no demo): the snapshot-for-study workhorse."""
    steps_budget = args.steps
    if steps_budget is None and not args.frames:
        steps_budget = 1_000_000
        print(f"(headless with no --steps/--frames: defaulting to --steps {steps_budget:,})")
    frame = 0
    status = "budget reached"
    while True:
        if args.frames and frame >= args.frames:
            break
        if steps_budget is not None and rt.cpu.instruction_count >= steps_budget:
            break
        new_status, keep_running = _step_frame(frontend, rt, args, frame)
        if new_status:
            status = new_status
        if not keep_running:
            break
        frame += 1
    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame)


# --- the viewer -----------------------------------------------------------------------------------

def run_view(frontend: GameFrontend, rt, args,
             playback: InputDemoPlayback | None = None) -> int:
    """The live pygame viewer: hybrid play, demo record/replay, F10/F11/F12."""
    try:
        import numpy as np
        import pygame
    except ImportError as exc:  # the viewer extras; the headless paths need neither
        missing = exc.name or "numpy/pygame"
        hint = ("pip install numpy pygame" if "pypy" not in sys.version.lower()
                else "pypy -m pip install numpy pygame-ce  "
                     "(the community fork; upstream pygame has no PyPy wheel)")
        raise SystemExit(
            f"the live viewer needs {missing!r}, which is not installed.\n"
            f"  install it:  {hint}\n"
            f"  or run without a window:  add --headless "
            f"(snapshots, demo replay and every verifier work headless)"
        ) from exc

    from dos_re.display import Display

    replaying = playback is not None
    pygame.init()
    first = np.asarray(frontend.decode_frame(rt), np.uint8)
    fh, fw = first.shape[:2]
    par = 1.0 if args.square_pixels else 1.2
    display = Display((fw * args.scale, int(fh * par) * args.scale),
                      title=frontend.window_title(args, "replay" if replaying else "live"))
    display.par = par
    scancodes = scancode_table(pygame)
    clock = pygame.time.Clock()

    frame_box = {"n": 0}
    recorder: dict[str, InputDemoRecorder | None] = {"rec": None}
    last_rgb = [first]

    def start_recording(name: str) -> None:
        rec = InputDemoRecorder(root=Path(args.demo_dir), name=name,
                                metadata=frontend.demo_metadata(args))
        out = rec.start(rt, boundary=frame_box["n"])
        recorder["rec"] = rec
        print(f"recording demo -> {out}")

    def stop_recording() -> None:
        rec = recorder["rec"]
        if rec is not None and rec.active:
            out = rec.stop(boundary=frame_box["n"])
            print(f"saved demo ({rec.event_count} events) -> {out}")
        recorder["rec"] = None

    def live_input(scancode: int) -> None:
        frontend.deliver_input(rt, scancode)
        rec = recorder["rec"]
        if rec is not None and rec.active:
            rec.record_scan(boundary=frame_box["n"], scancode=scancode)

    # Live mouse -> INT 33h driver state (real-mode DOSMachine.set_mouse_norm).
    # Only used by mouse-driven games; a no-op when the runtime has no such API.
    _set_mouse = getattr(rt.dos, "set_mouse_norm", None)
    mouse_btn = [0]  # Microsoft mask: bit0=left, bit1=right, bit2=middle
    mouse_norm = [None]  # latest host (u, v); None until the mouse first moves/clicks

    def feed_mouse(pos) -> None:
        # display.get_size() works for both the GPU-window and plain-surface
        # backends (pygame.display.get_surface() is None under the GPU path, so
        # relying on it would silently drop every click).
        if _set_mouse is None:
            return
        w, h = display.get_size()
        u = pos[0] / max(1, w - 1)
        v = pos[1] / max(1, h - 1)
        mouse_norm[0] = (u, v)
        if recorder["rec"] is None:
            # Live: apply immediately.  Quantized exactly like a recording, so
            # toggling recording on/off never changes where the pointer lands.
            _set_mouse(*mouse_sample(u, v, mouse_btn[0]))
        # While recording, application is deferred to the once-per-boundary
        # sample below so the VM sees exactly the recorded state (host events
        # only reach us between frames, so nothing is delayed by this).

    def sample_mouse_for_demo() -> None:
        # Once per frame while recording: record the deduped mouse sample keyed
        # to the same boundary as scancodes, and apply THE SAMPLE (not the
        # full-precision host value) to the VM.  Applied EVERY frame, changed or
        # not, because set_mouse_norm re-maps through the game's current INT 33h
        # range — replay mirrors this (the proven PM recorder/replay design).
        rec = recorder["rec"]
        if rec is None or not rec.active or _set_mouse is None or mouse_norm[0] is None:
            return
        u, v = mouse_norm[0]
        _set_mouse(*rec.record_mouse(boundary=frame_box["n"], u=u, v=v, buttons=mouse_btn[0]))

    def screenshot() -> None:
        rgb = last_rgb[0]
        if rgb is None:
            return
        h, w = rgb.shape[0], rgb.shape[1]
        surf = pygame.image.frombuffer(np.ascontiguousarray(rgb).tobytes(), (w, h), "RGB")
        out = frontend.artifacts_dir / f"shot_{frontend.name}_{datetime.now():%Y%m%d_%H%M%S}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surf, str(out))
        print(f"screenshot: {out}")

    dispatcher = KeyDispatcher(live_input)
    audio = frontend.create_audio_sink(pygame, rt, args)
    running = True
    status = "replaying" if replaying else "running"

    if not replaying and args.record_demo:
        start_recording(args.record_demo)

    try:
        while running and (args.frames == 0 or frame_box["n"] < args.frames):
            if args.steps is not None and rt.cpu.instruction_count >= args.steps:
                status = f"step budget reached ({args.steps:,})"
                break
            if replaying and playback.finished(frame_box["n"]):
                if args.demo_continue:
                    replaying = False
                    status = "demo finished -- live input"
                    print(status)
                else:
                    status = "demo replay complete"
                    break

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    display.resize(event.w, event.h)
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F12:
                    out = _timestamp_dir(frontend.artifacts_dir, f"snapshot_{frontend.name}")
                    write_snapshot(rt, out, status="manual viewer snapshot",
                                   steps=rt.cpu.instruction_count, trace_tail=())
                    print(f"snapshot: {out}")
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F10:
                    screenshot()
                elif replaying:
                    continue  # ignore host keys while a demo drives input
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
                    if recorder["rec"] is None:
                        start_recording(args.record_demo or frontend.name)
                    else:
                        stop_recording()
                elif event.type == pygame.KEYDOWN:
                    sc = scancodes.get(event.key)
                    if sc is not None:
                        dispatcher.post_down(sc)
                elif event.type == pygame.KEYUP:
                    sc = scancodes.get(event.key)
                    if sc is not None:
                        dispatcher.post_up(sc)
                elif event.type == pygame.MOUSEMOTION:
                    feed_mouse(event.pos)
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                    bit = {1: 0x01, 3: 0x02, 2: 0x04}.get(event.button)
                    if bit is not None:
                        if event.type == pygame.MOUSEBUTTONDOWN:
                            mouse_btn[0] |= bit
                        else:
                            mouse_btn[0] &= ~bit
                    feed_mouse(event.pos)

            if replaying:
                playback.apply_to_runtime(frame_box["n"], rt,
                                          deliver=lambda r, sc: frontend.deliver_input(r, sc))
            else:
                dispatcher.pump()
                sample_mouse_for_demo()

            new_status, keep_running = _step_frame(frontend, rt, args, frame_box["n"])
            if new_status:
                status = new_status
            running = running and keep_running

            if audio is not None:
                audio.pump()
            rgb = np.asarray(frontend.decode_frame(rt), np.uint8)
            last_rgb[0] = rgb
            display.draw_game(rgb)
            display.flip()
            pygame.display.set_caption(
                f"{frontend.window_title(args, 'replay' if replaying else 'live')} | {status} | "
                f"frame={frame_box['n']} steps={rt.cpu.instruction_count:,} | "
                f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X}"
                + (" | REC" if recorder["rec"] is not None else "")
            )
            frame_box["n"] += 1
            clock.tick(args.present_hz)
    finally:
        stop_recording()
        pygame.quit()

    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame_box["n"])


# --- entry point -----------------------------------------------------------------------------------

def _use_real_console_input(rt) -> None:
    """Make blocking DOS console reads (INT 21h AH=01h/07h/08h) wait for a real
    key instead of synthesizing Esc.

    DOSMachine defaults ``console_input_fallback`` to 0x011B (Esc) so a bare
    headless ``cpu.run()`` with no driver loop can't hang on a blocking read.
    But every player driver path (view, headless, replay) routes blocking reads
    through ``_step_frame``, which already catches ``ConsoleInputWouldBlock``
    and reports "waiting for DOS key" without hanging -- so the Esc synthesis
    is not needed here and is actively harmful: a game that reads menu keys via
    INT 21h AH=07h (SkyRoads does) receives a phantom Esc, interprets it as
    "quit", and calls exit(0). That presented as a spurious "program halted" at
    the main menu -- the game appearing to quit itself a few seconds in, with no
    real keypress. Clearing the fallback makes the read block for a real key,
    which the front-end (interactive) or demo/queue (headless/replay) supplies.
    """
    rt.dos.console_input_fallback = None


def main(frontend: GameFrontend, argv: list[str] | None = None,
         description: str | None = None) -> int:
    """The standard play.py main: parse the unified CLI and dispatch.

    ``python scripts/play.py`` -> live viewer (hybrid runtime).
    ``--headless``             -> bounded headless run (snapshot for study).
    ``--play-demo DIR``        -> replay (viewer unless --headless; +``--demo-continue``
                                  hands over to the player when the demo ends).
    """
    args = build_arg_parser(frontend, description).parse_args(argv)

    if args.play_demo:
        playback = InputDemoPlayback.load(args.play_demo)
        frontend.apply_demo_metadata(args, playback.manifest.get("metadata", {}))
        if playback.is_cold_start:
            rt = frontend.create_runtime(args)
        else:
            rt = frontend.load_snapshot_runtime(args, playback.snapshot_path())
        frontend.apply_hook_mode(rt, args)
        _use_real_console_input(rt)
        if args.headless:
            return run_replay_headless(frontend, rt, args, playback)
        return run_view(frontend, rt, args, playback=playback)

    if args.snapshot:
        rt = frontend.load_snapshot_runtime(args, args.snapshot)
    else:
        rt = frontend.create_runtime(args)
    frontend.apply_hook_mode(rt, args)
    _use_real_console_input(rt)
    if args.headless:
        return run_headless(frontend, rt, args)
    return run_view(frontend, rt, args)
