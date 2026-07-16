"""tiny_frame_game — the whole recovery lifecycle on a synthetic game, in one run.

Where ``examples/minimal_adapter`` shows the hook/verify/snapshot loop on a
straight-line program, this walkthrough runs every core mechanism against a
real *frame loop* (retrace wait, INT 09h keyboard ISR, framebuffer output):

  1. oracle run        — boot the EXE, step frame boundaries (dos_re.checkpoints)
  2. cold-start demo   — record input-only (no snapshot), replay from a fresh
                         boot, prove frame-by-frame framebuffer equality
  3. snapshot          — freeze mid-run, restore, prove both continuations agree
  4. wrong hook        — a subtly wrong draw routine is caught by the strict
                         differential hook verifier (full-memory diff)
  5. verified hook     — the correct recovered draw routine passes on every call
  6. frame verifier    — lockstep reference (pure ASM) vs candidate (hooked),
                         zero divergences; then a wrong candidate is caught
  7. state mirror      — human-named views over the game's memory (dos_re.state_view)

Run from the repo root:

    python examples/tiny_frame_game/walkthrough.py

No game assets, no dependencies. Read game.py for the synthetic program; read
the retired 1.0 starter's lifecycle docs (historical) for how these stages map onto a real port.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from game import (  # noqa: E402
    COUNTER,
    DRAW_FRAME,
    FRAME_LOOP_TOP,
    KEYSTATE,
    WAIT_HEAD,
    WIDTH,
    build_game_exe,
)

from dos_re.checkpoints import run_to_next_checkpoint  # noqa: E402
from dos_re.cpu import CPU8086  # noqa: E402
from dos_re.frame_verify import FrameVerifyConfig, make_frame_sample, run_frame_verifier  # noqa: E402
from dos_re.input_demo import InputDemoPlayback, InputDemoRecorder  # noqa: E402
from dos_re.interrupts import deliver_scancode  # noqa: E402
from dos_re.memory import linear  # noqa: E402
from dos_re.runtime import Runtime, create_runtime  # noqa: E402
from dos_re.snapshot import load_snapshot, write_snapshot  # noqa: E402
from dos_re.state_view import ByteBackend, StructView, U8  # noqa: E402
from dos_re.verification import HookVerifierConfig, HookVerifyDivergence, install_hook_verifier  # noqa: E402

# One demo scenario used by both record and replay: scancode delivered at frame.
DEMO_EVENTS = ((3, 0x1E), (6, 0x9E))  # 'A' make at frame 3, break at frame 6


def checkpoints_for(rt: Runtime) -> dict[tuple[int, int], str]:
    return {(rt.program.entry_cs, FRAME_LOOP_TOP): "frame: loop top"}


def boot(exe: Path) -> Runtime:
    """Boot and run the setup code to the FIRST frame boundary (no frame drawn yet)."""
    rt = create_runtime(exe)
    run_to_next_checkpoint(rt.cpu, checkpoints_for(rt), max_steps=100_000, skip_current=False)
    return rt


def advance_frame(rt: Runtime) -> None:
    run_to_next_checkpoint(rt.cpu, checkpoints_for(rt), max_steps=100_000)


def framebuffer_row(rt: Runtime) -> bytes:
    base = linear(0xA000, 0)
    return bytes(rt.cpu.mem.data[base:base + WIDTH])


# ---- stage 1: the oracle runs -------------------------------------------------------------------

def stage_oracle(exe: Path) -> list[bytes]:
    rt = boot(exe)
    assert rt.dos.video_mode == 0x13
    rows = []
    for _ in range(4):
        advance_frame(rt)
        rows.append(framebuffer_row(rt))
    assert [r[0] for r in rows] == [0, 1, 2, 3] and all(len(set(r)) == 1 for r in rows)
    print("[oracle]    boots to mode 13h; row colour follows the frame counter:",
          [r[0] for r in rows])
    return rows


# ---- stage 2: cold-start demo record + replay ---------------------------------------------------

def run_session(rt: Runtime, frames: int, playback: InputDemoPlayback | None = None,
                recorder: InputDemoRecorder | None = None) -> list[bytes]:
    """THE shared driver: one boundary definition for recording and replay.

    (Different drivers with different boundary definitions are the classic way
    demo proofs silently rot — see docs/demos_and_snapshots.md.)"""
    rows = []
    events = dict(DEMO_EVENTS)
    for frame in range(frames):
        if playback is not None:
            playback.apply_to_runtime(frame, rt)
        elif recorder is not None and frame in events:
            deliver_scancode(rt, events[frame])
            recorder.record_scan(boundary=frame, scancode=events[frame])
        advance_frame(rt)
        rows.append(framebuffer_row(rt))
    return rows


def stage_cold_start_demo(exe: Path, tmp: Path) -> None:
    # Record: input-only capture from power-on — no start snapshot at all.
    rt = boot(exe)
    recorder = InputDemoRecorder(root=tmp, name="cold", metadata={"video": "mode13h"})
    demo_dir = recorder.start(rt, boundary=0, write_start_snapshot=False)
    recorded = run_session(rt, 10, recorder=recorder)
    recorder.stop(boundary=10)

    # Replay: boot a FRESH runtime and feed only the recorded events.
    playback = InputDemoPlayback.load(demo_dir)
    assert playback.is_cold_start
    rt2 = boot(exe)
    replayed = run_session(rt2, 10, playback=playback)

    assert recorded == replayed, "cold-start demo replay diverged from the recording run"
    assert recorded[2][0] != recorded[4][0] - 2, "input visibly changed the output"
    print(f"[demo]      cold-start demo (no snapshot) replays 10 frames byte-identically; "
          f"key at frame 3 shifts colour {recorded[2][0]} -> {recorded[3][0]}")


# ---- stage 3: snapshot determinism --------------------------------------------------------------

def stage_snapshot(exe: Path, tmp: Path) -> None:
    rt = boot(exe)
    for _ in range(3):
        advance_frame(rt)
    snap = tmp / "snap_mid"
    write_snapshot(rt, snap, status="tiny_frame_game mid-run", steps=rt.cpu.instruction_count,
                   trace_tail=())
    restored = load_snapshot(exe, snap)
    for r in (rt, restored):
        for _ in range(3):
            advance_frame(r)
    assert framebuffer_row(rt) == framebuffer_row(restored)
    print("[snapshot]  restored runtime's continuation matches the live one, frame for frame")


# ---- stages 4+5: wrong hook caught, correct hook verified ---------------------------------------

def _draw_frame_hook(fill_width: int):
    """The 'recovered' DRAW_FRAME routine as a thin hook (near-RET boundary)."""
    def hook(cpu: CPU8086) -> None:
        colour = (cpu.mem.rb(cpu.s.ds, COUNTER) + cpu.mem.rb(cpu.s.ds, KEYSTATE)) & 0xFF
        base = linear(0xA000, 0)
        for i in range(fill_width):
            cpu.mem.data[base + i] = colour
        cpu.set_reg8(0, colour)                 # AL holds the colour after the adds
        cpu.s.cx = 0                            # REP STOSB leaves CX=0 ...
        cpu.s.di = WIDTH                        # ... and DI past the row
        cpu.set_logic_flags(0, 16)              # final flags come from XOR DI,DI
        cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)   # RET
        cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF
    return hook


def stage_hooks(exe: Path) -> None:
    # Wrong: fills one byte short. Registers match; only full-memory diff sees it.
    rt = boot(exe)
    key = (rt.program.entry_cs, DRAW_FRAME)
    rt.cpu.replacement_hooks[key] = _draw_frame_hook(WIDTH - 1)
    rt.cpu.hook_names[key] = "wrong_draw_row"
    install_hook_verifier(rt, HookVerifierConfig.strict(verify_all=True), stops={})
    try:
        for _ in range(3):
            advance_frame(rt)
    except HookVerifyDivergence as exc:
        first = [ln for ln in str(exc).splitlines() if "Memory differences" in ln or "byte" in ln]
        print(f"[verifier]  off-by-one draw hook caught by the FULL-MEMORY diff "
              f"({first[0].strip() if first else 'memory divergence'})")
    else:
        raise AssertionError("the verifier failed to catch the off-by-one hook")

    # Correct: verified against the interpreted original on every single call.
    rt = boot(exe)
    key = (rt.program.entry_cs, DRAW_FRAME)
    rt.cpu.replacement_hooks[key] = _draw_frame_hook(WIDTH)
    rt.cpu.hook_names[key] = "recovered_draw_row"
    install_hook_verifier(rt, HookVerifierConfig.strict(verify_all=True), stops={})
    for _ in range(5):
        advance_frame(rt)
    assert framebuffer_row(rt)[0] == 4
    print("[hybrid]    recovered draw routine ran 5 frames, every call verified vs the ASM oracle")


# ---- stage 6: the frame verifier ----------------------------------------------------------------

def _boundary_hook(cpu: CPU8086) -> None:
    """Thin replacement for the boundary instruction (MOV DX,03DAh) at FRAME_LOOP_TOP."""
    cpu.s.dx = 0x03DA
    cpu.s.ip = WAIT_HEAD


def _install_boundary(rt: Runtime) -> tuple[int, int]:
    key = (rt.program.entry_cs, FRAME_LOOP_TOP)
    rt.cpu.replacement_hooks[key] = _boundary_hook
    rt.cpu.hook_names[key] = "frame_boundary"
    return key


def _sample_builder(rt, side, frame_no, kind, hook, boundary_steps, start, recent,
                    recent_sample_changes=()):
    row = framebuffer_row(rt)
    rgb = bytes(c for px in row for c in (px, px, px))  # grayscale, for the diff PNGs
    return make_frame_sample(rt=rt, side=side, frame_no=frame_no, kind=kind, hook=hook,
                             boundary_steps=boundary_steps, start_count=start,
                             recent_hooks=recent, raw=row, rgb=rgb, width=WIDTH, height=1,
                             context="tiny")


def stage_frame_verifier(exe: Path, tmp: Path) -> None:
    def lockstep(candidate_fill: int) -> int:
        reference = create_runtime(exe)
        candidate = create_runtime(exe)
        boundary = _install_boundary(reference)
        _install_boundary(candidate)
        draw = (candidate.program.entry_cs, DRAW_FRAME)
        candidate.cpu.replacement_hooks[draw] = _draw_frame_hook(candidate_fill)
        candidate.cpu.hook_names[draw] = "candidate_draw_row"
        config = FrameVerifyConfig(max_frames=6, frame_budget=100_000, source="vram",
                                   dump_dir=tmp / "frame_verify", preview_on_diff=False,
                                   log_every=0)
        return run_frame_verifier(
            reference=reference, candidate=candidate, config=config,
            boundary_hooks=((boundary, "frame"),), sample_builder=_sample_builder,
            reference_env_hooks={boundary},
        )

    assert lockstep(WIDTH) == 0
    print("[frames]    lockstep ASM-vs-hooked frame verification: 6 frames, 0 divergences")
    diverged = lockstep(WIDTH - 1)
    assert diverged != 0
    print(f"[frames]    wrong candidate detected at frame {diverged} "
          f"(diff artifacts dumped for inspection)")


# ---- stage 7: the state mirror ------------------------------------------------------------------

class TinyGameView(StructView):
    """The game's state behind human names — offsets live HERE, nowhere else."""

    counter = U8(COUNTER)
    keystate = U8(KEYSTATE)

    def __init__(self, rt: Runtime):
        super().__init__(ByteBackend(rt.cpu.mem, base=rt.program.entry_cs << 4), 0)


def stage_state_mirror(exe: Path) -> None:
    rt = boot(exe)
    for _ in range(3):
        advance_frame(rt)
    deliver_scancode(rt, 0x1E)
    advance_frame(rt)

    view = TinyGameView(rt)
    assert view.counter == 4 and view.keystate == 0x1E
    assert framebuffer_row(rt)[0] == (view.counter - 1 + view.keystate) & 0xFF
    view.keystate = 0            # views write through to the same bytes
    assert rt.cpu.mem.rb(rt.program.entry_cs, KEYSTATE) == 0
    print(f"[mirror]    recovered-style code reads view.counter={view.counter}, "
          f"view.keystate -- the same bytes the oracle verifies")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        exe = build_game_exe(tmp / "TINY.EXE")
        stage_oracle(exe)
        stage_cold_start_demo(exe, tmp)
        stage_snapshot(exe, tmp)
        stage_hooks(exe)
        stage_frame_verifier(exe, tmp)
        stage_state_mirror(exe)
    print("walkthrough complete: oracle, cold-start demo, snapshot, hook oracle, "
          "frame oracle, state mirror -- all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
