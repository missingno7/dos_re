"""Several dos_re 3.0 capabilities demonstrated on a synthetic frame loop.

The example combines stable identities, retained Recovery IR, ReplayArtifact,
Execution Atlas queries, mixed-plan construction, continuation restore,
differential verification, and a typed DOS-memory view. They run in one useful
order so this file doubles as an integration test; the order is not a required
port workflow, and each mechanism remains independently usable.

Run from the repo root:

    python examples/tiny_frame_game/walkthrough.py

No game assets and no external dependencies are required. Read ``game.py`` for
the synthetic program and ``docs/getting_started.md`` for the composable model.
"""
from __future__ import annotations

import hashlib
import json
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

from dos_re.atlas import ExecutionAtlas  # noqa: E402
from dos_re.cpu import CPU8086  # noqa: E402
from dos_re.execution import (BuildTarget, ImplementationCatalog,
                              ImplementationDescriptor, ImplementationEntry,
                              ImplementationOrigin, NativeBootstrapProvider,
                              OverrideCategory, ProgramCoverage, plan_execution,
                              profile_configuration)  # noqa: E402
from dos_re.frame_verify import FrameVerifyConfig, make_frame_sample, run_frame_verifier  # noqa: E402
from dos_re.replay_input import RealModeInputAdapter, SCAN_CHANNEL, scan_payload  # noqa: E402
from dos_re.interrupts import deliver_scancode  # noqa: E402
from dos_re.identity import (FunctionIdentity, ImageIdentity, ProgramIdentity,
                             real_mode_address)  # noqa: E402
from dos_re.memory import linear  # noqa: E402
from dos_re.player import GameFrontend  # noqa: E402
from dos_re.runtime import Runtime, create_runtime  # noqa: E402
from dos_re.replay import (FunctionVisitIndex, ReplayArtifact,
                           ReplayExecutionIdentity, ReplayPoint,
                           ReplayRecording)  # noqa: E402
from dos_re.snapshot import (apply_runtime_continuation, capture_runtime_continuation,
                             load_snapshot, write_snapshot)  # noqa: E402
from dos_re.state_view import ByteBackend, StructView, U8  # noqa: E402
from dos_re.verification import HookVerifierConfig, HookVerifyDivergence, install_hook_verifier  # noqa: E402

# One scenario used by both record and replay: scancode delivered at frame.
REPLAY_EVENTS = ((3, 0x1E), (6, 0x9E))  # 'A' make at frame 3, break at frame 6


def _run_to_frame_boundary(
    rt: Runtime, *, skip_current: bool, max_steps: int = 100_000,
) -> None:
    """Example backend adapter for the replay's stable frame point."""
    target = (rt.program.entry_cs, FRAME_LOOP_TOP)
    if skip_current:
        rt.cpu.step()
    for _ in range(max_steps):
        if rt.cpu.addr() == target:
            return
        rt.cpu.step()
    raise TimeoutError(f"frame boundary {target!r} not reached")


def boot(exe: Path) -> Runtime:
    """Boot and run the setup code to the FIRST frame boundary (no frame drawn yet)."""
    rt = create_runtime(exe)
    _run_to_frame_boundary(rt, skip_current=False)
    return rt


def advance_frame(rt: Runtime) -> None:
    _run_to_frame_boundary(rt, skip_current=True)


def framebuffer_row(rt: Runtime) -> bytes:
    base = linear(0xA000, 0)
    return bytes(rt.cpu.mem.data[base:base + WIDTH])


# ---- capability: run the original oracle ---------------------------------------------------------

def demonstrate_oracle(exe: Path) -> list[bytes]:
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


# ---- capability: ReplayArtifact record and replay ------------------------------------------------

def run_session(rt: Runtime, frames: int, playback: RealModeInputAdapter | None = None,
                recorder: ReplayRecording | None = None) -> list[bytes]:
    """THE shared driver: one boundary definition for recording and replay.

    (Different drivers with different boundary definitions are the classic way
    replay proofs silently rot — see docs/replay_architecture.md.)"""
    rows = []
    events = dict(REPLAY_EVENTS)
    for frame in range(frames):
        if playback is not None:
            playback.apply_to_runtime(frame, rt)
        elif recorder is not None and frame in events:
            deliver_scancode(rt, events[frame])
            recorder.add(frame, SCAN_CHANNEL, scan_payload(events[frame]))
        advance_frame(rt)
        rows.append(framebuffer_row(rt))
    return rows


def demonstrate_replay_artifact(
    exe: Path, tmp: Path, function_id: str,
) -> ReplayArtifact:
    # Record from a stable frame seam with a complete embedded base.
    rt = boot(exe)
    profile = ReplayExecutionIdentity(
        "tiny-oracle", "oracle", "tiny-frame-interpreter",
        hashlib.sha256(exe.read_bytes()).hexdigest(),
        "example-runtime-v1", "example-devices-v1",
        "dos-re-real-mode-continuation-v1", "tiny-machine-v1")
    recorder = ReplayRecording(
        tmp / "tiny-replay", timeline_id="tiny-frame-boundaries-v1",
        profile=profile, base_state=capture_runtime_continuation(rt, event_cursor=0),
        metadata={"video": "mode13h"})
    recorded = run_session(rt, 10, recorder=recorder)
    artifact = recorder.finish(
        10, end_state=capture_runtime_continuation(
            rt, event_cursor=recorder.event_count))
    visits = FunctionVisitIndex()
    for frame in range(10):
        visits.enter(function_id, ReplayPoint(frame, artifact.timeline_id))
        visits.exit(function_id, ReplayPoint(frame + 1, artifact.timeline_id))
    artifact.set_function_visits(visits)

    # Replay: boot a FRESH runtime and feed only the recorded events.
    rt2 = boot(exe)
    base = artifact.restore(profile, ReplayPoint(0, artifact.timeline_id))
    apply_runtime_continuation(rt2, base)
    playback = RealModeInputAdapter(artifact.events, event_cursor=base.event_cursor)
    replayed = run_session(rt2, 10, playback=playback)

    assert recorded == replayed, "ReplayArtifact diverged from the recording run"
    assert recorded[2][0] != recorded[4][0] - 2, "input visibly changed the output"
    print(f"[replay]    embedded-base replay runs 10 frames byte-identically; "
          f"key at frame 3 shifts colour {recorded[2][0]} -> {recorded[3][0]}")
    return artifact


# ---- capability: combine retained IR, replay, Atlas, and plans -----------------------------------

def stable_program_identity(exe: Path) -> tuple[ProgramIdentity, ImageIdentity, str]:
    program = ProgramIdentity("tiny-frame-game:1")
    image = ImageIdentity(
        program, "TINY.EXE", "sha256", hashlib.sha256(exe.read_bytes()).hexdigest())
    rt = boot(exe)
    function = FunctionIdentity(
        image, "real-mode", real_mode_address(rt.program.entry_cs, DRAW_FRAME))
    return program, image, str(function)


def write_minimal_recovery_ir(exe: Path, path: Path) -> None:
    """Retain the recovered draw-function skeleton; Atlas never decodes the EXE."""
    rt = boot(exe)
    entry = f"{rt.program.entry_cs:04X}:{DRAW_FRAME:04X}"
    path.write_text(json.dumps({
        "ir_version": 0,
        "functions": {
            entry: {
                "entry": entry,
                "symbol": "draw_frame",
                "liftable": True,
                "refusals": [],
                "blocks": [],
                "exits": [],
                "signature": "",
            },
        },
    }, sort_keys=True), encoding="utf-8")


def draw_catalog(
    target: str, implementation_id: str, fill_width: int,
) -> ImplementationCatalog:
    body = _draw_frame_body(fill_width)

    def activate(runtime, targets):
        assert targets == (target,)
        address = (runtime.program.entry_cs, DRAW_FRAME)

        def cpu_adapter(cpu: CPU8086) -> None:
            colour, row = body(
                cpu.mem.rb(cpu.s.ds, COUNTER),
                cpu.mem.rb(cpu.s.ds, KEYSTATE),
            )
            base = linear(0xA000, 0)
            cpu.mem.data[base:base + len(row)] = row
            cpu.set_reg8(0, colour)
            cpu.s.cx = 0
            cpu.s.di = WIDTH
            cpu.set_logic_flags(0, 16)
            cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)
            cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF

        runtime.cpu.replacement_hooks[address] = cpu_adapter
        runtime.cpu.hook_names[address] = implementation_id

    return ImplementationCatalog((ImplementationEntry(
        ImplementationDescriptor(
            implementation_id=implementation_id,
            targets=frozenset({target}),
            origin=ImplementationOrigin.AUTHORED,
            category=OverrideCategory.FAITHFUL,
            properties=frozenset({"cpu-adapted", "dos-memory-backed"}),
            implementation_digest=f"{implementation_id}:{fill_width}",
        ),
        implementation=body,
        activate=activate,
    ),))


def demonstrate_atlas_and_planning(
    exe: Path, tmp: Path, artifact: ReplayArtifact, program: ProgramIdentity,
    image: ImageIdentity, function_id: str,
) -> ProgramCoverage:
    ir_path = tmp / "recovery_ir.json"
    write_minimal_recovery_ir(exe, ir_path)
    atlas = ExecutionAtlas.create(
        tmp / "atlas", program=program,
        product_roots={"game": (function_id,)})
    atlas.import_recovery_ir(ir_path, image=image)
    atlas.ingest_replay(artifact.directory)
    atlas.validate()
    coverage = atlas.coverage_for("game")
    visit = atlas.best_replay(function_id)
    assert visit.invocation_count == 10

    catalog = draw_catalog(function_id, "recovered_draw_row", WIDTH)
    development = plan_execution(
        profile_configuration(
            "development", program_identity=str(program),
            product_profile="game", selected_overrides=("recovered_draw_row",),
            bootstrap_provider=NativeBootstrapProvider(
                provider_id="example-native-bootstrap",
                state_outputs=("tiny machine state",),
                provider_digest="example-native-bootstrap-v1",
            ),
        ),
        coverage, catalog,
    )
    release = plan_execution(
        profile_configuration(
            "release", program_identity=str(program), product_profile="game",
            selected_overrides=("recovered_draw_row",),
            bootstrap_provider=NativeBootstrapProvider(
                provider_id="example-native-bootstrap",
                state_outputs=("tiny machine state",),
                provider_digest="example-native-bootstrap-v1",
            ),
            build_target=BuildTarget("portable", "directory"),
        ),
        coverage, catalog,
    )
    assert release.report.package_ready
    assert release.report.is_detached_from("original-exe")
    print("[atlas]     retained IR + replay evidence: draw_frame visited "
          f"{visit.invocation_count} times")
    print("[planning]  development and release use the same identity/catalog; "
          "release is EXE-detached and package-ready")
    return coverage


# ---- capability: snapshot determinism ------------------------------------------------------------

def demonstrate_snapshot(exe: Path, tmp: Path) -> None:
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


# ---- capability: wrong adapter caught, correct implementation verified ---------------------------

def _draw_frame_body(fill_width: int):
    """Natural authored behavior, independent of CPU control flow."""
    def draw(counter: int, keystate: int) -> tuple[int, bytes]:
        colour = (counter + keystate) & 0xFF
        return colour, bytes([colour]) * fill_width
    return draw


def _bind_draw_implementation(
    rt: Runtime, target: str, coverage: ProgramCoverage,
    implementation_id: str, fill_width: int,
) -> None:
    catalog = draw_catalog(target, implementation_id, fill_width)
    plan = plan_execution(
        profile_configuration(
            "development",
            program_identity="tiny-frame-game",
            selected_overrides=(implementation_id,),
        ),
        coverage,
        catalog,
    )
    GameFrontend(ROOT).bind_execution_plan(rt, plan)


def demonstrate_focused_verification(
    exe: Path, function_id: str, coverage: ProgramCoverage,
) -> None:
    # Wrong: fills one byte short. Registers match; only full-memory diff sees it.
    rt = boot(exe)
    _bind_draw_implementation(
        rt, function_id, coverage, "wrong_draw_row", WIDTH - 1)
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
    _bind_draw_implementation(
        rt, function_id, coverage, "recovered_draw_row", WIDTH)
    install_hook_verifier(rt, HookVerifierConfig.strict(verify_all=True), stops={})
    for _ in range(5):
        advance_frame(rt)
    assert framebuffer_row(rt)[0] == 4
    print("[hybrid]    recovered draw routine ran 5 frames, every call verified vs the ASM oracle")


# ---- capability: frame verification --------------------------------------------------------------

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


def demonstrate_frame_verifier(
    exe: Path, tmp: Path, function_id: str, coverage: ProgramCoverage,
) -> None:
    def lockstep(candidate_fill: int) -> int:
        reference = create_runtime(exe)
        candidate = create_runtime(exe)
        boundary = _install_boundary(reference)
        _install_boundary(candidate)
        _bind_draw_implementation(
            candidate, function_id, coverage,
            "candidate_draw_row", candidate_fill)
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


# ---- capability: typed state view ---------------------------------------------------------------

class TinyGameView(StructView):
    """The game's state behind human names — offsets live HERE, nowhere else."""

    counter = U8(COUNTER)
    keystate = U8(KEYSTATE)

    def __init__(self, rt: Runtime):
        super().__init__(ByteBackend(rt.cpu.mem, base=rt.program.entry_cs << 4), 0)


def demonstrate_state_view(exe: Path) -> None:
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
        program, image, function_id = stable_program_identity(exe)
        demonstrate_oracle(exe)
        artifact = demonstrate_replay_artifact(exe, tmp, function_id)
        coverage = demonstrate_atlas_and_planning(
            exe, tmp, artifact, program, image, function_id)
        demonstrate_snapshot(exe, tmp)
        demonstrate_focused_verification(exe, function_id, coverage)
        demonstrate_frame_verifier(exe, tmp, function_id, coverage)
        demonstrate_state_view(exe)
    print("walkthrough complete: identity, IR, replay, Atlas, planning, "
          "detachment, verification, and state projection -- all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
