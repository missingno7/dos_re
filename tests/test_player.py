"""Contract tests for dos_re.player — the game-agnostic play-runner core.

These are pure-logic tests: no numpy/pygame needed (the CLI + dispatch layer
must work headless without the viewer extras installed; see the lazy-import
subprocess check).  The full-stack viewer path is covered by
test_view_smoke.py on machines with the extras.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dos_re import player
from dos_re.execution import (
    BackendAdapter,
    GENERATED_CPULESS_CARRIER,
    INTERPRETED_CPU_CARRIER,
    DependencyCapability,
    NativeBootstrapProvider,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    OverrideCategory,
    ProgramCoverage,
    profile_configuration,
)
from dos_re.player import GameFrontend, build_arg_parser
from dos_re.replay import (
    GUEST_INSTRUCTION_COORDINATE,
    ReplayError,
    ReplayPoint,
    ReplayPointCoordinate,
)

ROOT = Path(__file__).resolve().parents[1]


def _parse(frontend: GameFrontend, argv: list[str]):
    return build_arg_parser(frontend).parse_args(argv)


def test_import_stays_lazy_about_viewer_deps():
    """Planning imports no viewer, interpreter, or EXE-loader dependency."""
    code = ("import sys; import dos_re.player; "
            "bad = [m for m in "
            "('numpy', 'pygame', 'dos_re.cpu', 'dos_re.runtime') "
            "if m in sys.modules]; "
            "assert not bad, f'dos_re.player eagerly imported {bad}'")
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


def test_source_tree_identity_is_location_independent_and_byte_sensitive(
    tmp_path,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "nested").mkdir(parents=True)
        (root / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "nested" / "device.py").write_text(
            "DEVICE = 'pic'\n", encoding="utf-8"
        )

    original = player._source_tree_identity(first)
    assert original == player._source_tree_identity(second)
    (second / "nested" / "device.py").write_text(
        "DEVICE = 'sound-blaster'\n", encoding="utf-8"
    )
    assert original != player._source_tree_identity(second)


def test_standard_cli_defaults():
    fe = GameFrontend(ROOT)
    args = _parse(fe, [])
    assert args.headless is False           # viewer is the default state
    assert args.profile == "development"
    assert args.plan_only is False
    assert args.play_replay is None
    assert args.replay_continue is False
    assert args.steps_per_frame == GameFrontend.default_steps_per_frame
    assert args.timer_irqs_per_frame == GameFrontend.default_timer_irqs_per_frame
    assert args.present_hz == GameFrontend.default_present_hz
    assert args.snapshot is None and args.save_snapshot is None
    assert args.dos_args == ""
    assert args.verify_mode == "checkpointed"
    assert args.verify_checkpoint_span == 64
    assert args.verify_observables is False


def test_replay_frame_stops_on_guest_coordinate_not_dispatch_count():
    frontend = GameFrontend(ROOT)
    args = SimpleNamespace(timer_irqs_per_frame=0)

    class CPU:
        instruction_count = 10

        def step(self):
            self.instruction_count += 3

    runtime = SimpleNamespace(cpu=CPU())
    point = ReplayPoint(1, "test-timeline")
    coordinate = ReplayPointCoordinate(
        point, GUEST_INSTRUCTION_COORDINATE, 16)

    frontend.advance_replay_frame(runtime, args, 0, coordinate)
    assert runtime.cpu.instruction_count == 16

    runtime.cpu.instruction_count = 10
    with pytest.raises(ReplayError, match="crossed replay point"):
        frontend.advance_replay_frame(
            runtime,
            args,
            0,
            ReplayPointCoordinate(
                point, GUEST_INSTRUCTION_COORDINATE, 15),
        )
def test_frontend_defaults_flow_into_parser():
    class Fe(GameFrontend):
        name = "tinygame"
        default_exe = "assets/TINY.EXE"
        default_steps_per_frame = 123
        default_timer_irqs_per_frame = 2
        default_present_hz = 70

        def add_arguments(self, parser):
            parser.add_argument("--tiny-extra", action="store_true")

    args = _parse(Fe(ROOT), ["--tiny-extra"])
    assert args.exe == "assets/TINY.EXE"
    assert args.steps_per_frame == 123
    assert args.timer_irqs_per_frame == 2
    assert args.present_hz == 70
    assert args.tiny_extra is True


def test_replay_metadata_roundtrip():
    fe = GameFrontend(ROOT)
    args = _parse(fe, ["--exe", "GAME.EXE", "--steps-per-frame", "555",
                       "--timer-irqs-per-frame", "3"])
    meta = fe.replay_metadata(args)
    assert meta["steps_per_frame"] == 555 and meta["timer_irqs_per_frame"] == 3

    fresh = _parse(fe, ["--exe", "GAME.EXE"])   # defaults, as a replay would start
    fe.apply_replay_metadata(fresh, meta)
    assert fresh.steps_per_frame == 555 and fresh.timer_irqs_per_frame == 3


def test_replay_metadata_is_applied_before_plan_selection(monkeypatch):
    artifact = SimpleNamespace(metadata={"captured_product": "island"})
    monkeypatch.setattr(
        player.ReplayArtifact, "open", lambda path: artifact,
    )

    class Fe(GameFrontend):
        def apply_replay_metadata(self, args, metadata):
            args.captured_product = metadata["captured_product"]

        def resolve_execution_plan(self, args):
            assert args.captured_product == "island"
            return SimpleNamespace(
                configuration=SimpleNamespace(
                    verification_policy=SimpleNamespace(mode="none"),
                ),
            )

        def launch(self, args, plan):
            assert args.captured_product == "island"
            return 0

    assert player.main(Fe(ROOT), ["--play-replay", "recording"]) == 0


def test_replay_launch_allows_a_successor_runtime_profile(monkeypatch):
    recorded = SimpleNamespace(
        profile_id="recorded",
        role="candidate",
        implementation="implementation-before-fix",
        image="image-v1",
        runtime="runtime-before-fix",
        devices="devices-v1",
        continuation_schema="continuation-v1",
        projection_schema="projection-v1",
    )
    current = SimpleNamespace(
        profile_id="successor",
        role="candidate",
        implementation="implementation-after-fix",
        image="image-v1",
        runtime="runtime-after-fix",
        devices="devices-v1",
        continuation_schema="continuation-v1",
        projection_schema="projection-v1",
    )
    base = SimpleNamespace(event_cursor=0)
    registered = []

    class Artifact:
        metadata = {}
        timeline_id = "timeline-v1"

        def restore(self, profile, point):
            assert profile is recorded and point.ordinal == 0
            return base

        def profiles(self):
            return ((recorded, ReplayPoint(0, self.timeline_id)),)

        def register_profile(self, profile, *, base_point, base_state):
            registered.append((profile, base_point, base_state))

        def require_profile(self, profile):
            raise AssertionError("successor profile must be materialized")

        def timeline_coordinate(self, point):
            return ReplayPointCoordinate(point, "semantic-boundary-v1", {})

    playback = SimpleNamespace(
        artifact=Artifact(),
        capture_profile=recorded,
        profile=recorded,
        adapter=SimpleNamespace(seek=lambda cursor: None),
        select_profile=lambda profile: None,
    )
    monkeypatch.setattr(
        player._RealReplayPlayback, "open", lambda path: playback,
    )
    monkeypatch.setattr(
        player, "run_replay_headless",
        lambda frontend, runtime, args, opened: 17,
    )

    runtime = SimpleNamespace(
        dos=SimpleNamespace(console_input_fallback=0x011B),
    )

    class Fe:
        def apply_replay_metadata(self, args, metadata):
            pass

        def apply_replay_state(self, restored_runtime, state):
            assert restored_runtime is runtime and state is base

        def replay_profile(self, args, restored_runtime):
            assert restored_runtime is runtime
            return current

        def materialize_replay_profile_base(
            self, args, restored_runtime, artifact, *, source_profile,
            requested_profile, source_state,
        ):
            assert restored_runtime is runtime
            assert source_profile is recorded
            assert requested_profile is current
            return source_state

    args = SimpleNamespace(
        play_replay="recording",
        headless=True,
        execution_plan=object(),
    )
    assert player.launch_real_mode(
        Fe(), args,
        create_runtime=lambda parsed: runtime,
        load_snapshot_runtime=lambda parsed, path: runtime,
        bind_execution_plan=lambda restored_runtime: None,
    ) == 17
    assert registered == [(current, ReplayPoint(0, "timeline-v1"), base)]
    assert runtime.dos.console_input_fallback is None


def test_replay_launch_uses_an_existing_requested_profile_base(monkeypatch):
    recorded = SimpleNamespace(
        profile_id="recorded", role="candidate", implementation="capture",
        image="image", runtime="runtime", devices="capture-devices",
        continuation_schema="continuation", projection_schema="projection",
    )
    current = SimpleNamespace(
        profile_id="oracle", role="oracle", implementation="oracle",
        image="image", runtime="runtime", devices="oracle-devices",
        continuation_schema="continuation", projection_schema="projection",
    )
    capture_base = SimpleNamespace(event_cursor=0)
    oracle_base = SimpleNamespace(event_cursor=3)

    class Artifact:
        metadata = {}
        timeline_id = "timeline"

        def profiles(self):
            return ((recorded, 0), (current, 0))

        def require_profile(self, profile):
            assert profile is current

        def restore(self, profile, point):
            assert point.ordinal == 0
            return oracle_base if profile is current else capture_base

        def timeline_coordinate(self, point):
            return ReplayPointCoordinate(point, "semantic-boundary-v1", {})

    playback = SimpleNamespace(
        artifact=Artifact(), capture_profile=recorded, profile=recorded,
        adapter=SimpleNamespace(seek=lambda cursor: None),
        select_profile=lambda profile: None,
    )
    monkeypatch.setattr(player._RealReplayPlayback, "open", lambda path: playback)
    monkeypatch.setattr(player, "run_replay_headless", lambda *items: 23)
    runtime = SimpleNamespace(dos=SimpleNamespace(console_input_fallback=1))

    class Fe:
        apply_replay_metadata = staticmethod(lambda args, metadata: None)
        replay_profile = staticmethod(lambda args, rt: current)

        @staticmethod
        def apply_replay_state(rt, state):
            assert state is oracle_base

        @staticmethod
        def materialize_replay_profile_base(*args, **kwargs):
            raise AssertionError("an exact profile base must be restored directly")

    args = SimpleNamespace(
        play_replay="recording", headless=True, execution_plan=object(),
        composition="oracle",
    )
    assert player.launch_real_mode(
        Fe(), args, create_runtime=lambda args: runtime,
        load_snapshot_runtime=lambda args, path: runtime,
        bind_execution_plan=lambda rt: None,
    ) == 23


def test_standard_io_options_declare_plan_capabilities():
    fe = GameFrontend(ROOT)
    replay_args = _parse(fe, ["--play-replay", "replay"])
    assert fe.requested_capabilities(replay_args) == frozenset({
        DependencyCapability.REPLAY.value,
        DependencyCapability.SNAPSHOTS.value,
    })

    snapshot_args = _parse(fe, ["--snapshot", "snapshot"])
    assert fe.requested_capabilities(snapshot_args) == frozenset({
        DependencyCapability.SNAPSHOTS.value,
    })


def test_replay_device_identity_includes_optional_device_topology():
    class Device:
        base = 0x220
        irq = 7
        dma = 1

        def __init__(self, *, detection_only=False):
            self.detection_only = detection_only

    fe = GameFrontend(ROOT)
    args = _parse(fe, [])
    silent = SimpleNamespace(
        dos=SimpleNamespace(pic=None, sound_blaster=None),
    )
    detected = SimpleNamespace(
        dos=SimpleNamespace(
            pic=Device(), sound_blaster=Device(detection_only=True),
        ),
    )
    captured = SimpleNamespace(
        dos=SimpleNamespace(
            pic=Device(), sound_blaster=Device(detection_only=False),
        ),
    )
    assert fe.replay_device_identity(args, silent) != fe.replay_device_identity(
        args, detected
    )
    assert fe.replay_device_identity(args, detected) != fe.replay_device_identity(
        args, captured
    )


def test_frontend_selects_the_replay_projection_schema(monkeypatch):
    class Fe(GameFrontend):
        def replay_projection_schema(self, args, rt):
            return "game-semantic-v1"

    monkeypatch.setattr(player, "execution_composition_digest", lambda plan: "plan")
    fe = Fe(ROOT)
    args = _parse(fe, [])
    args.execution_plan = SimpleNamespace(
        implementations=(SimpleNamespace(
            origin=ImplementationOrigin.INTERPRETED,
        ),),
        configuration=SimpleNamespace(
            selected_overrides=(), profile="development",
        ),
    )
    runtime = SimpleNamespace(
        dos=SimpleNamespace(pic=None, sound_blaster=None),
    )
    assert fe.replay_profile(args, runtime).projection_schema == "game-semantic-v1"


class _StubCPU:
    def __init__(self):
        self.replacement_hooks = {}
        self.hook_names = {}


class _StubRuntime:
    def __init__(self):
        self.cpu = _StubCPU()


def test_detached_profile_fails_before_runtime_construction(capsys):
    class Fe(GameFrontend):
        created = False

        def create_runtime(self, args):
            self.created = True
            raise AssertionError("strict planning must happen before boot")

    fe = Fe(ROOT)
    assert player.main(fe, ["--profile", "detached", "--headless"]) == 2
    assert not fe.created
    assert "required capabilities forbidden" in capsys.readouterr().err


def test_configuration_errors_are_reported_without_a_traceback(capsys):
    class Fe(GameFrontend):
        def resolve_execution_plan(self, args):
            raise ValueError("invalid implementation composition")

    assert player.main(Fe(ROOT), ["--headless"]) == 2
    assert (
        capsys.readouterr().err
        == "error: invalid implementation composition\n"
    )


def test_frontend_can_declare_exe_free_implementation_for_same_player():
    class Fe(GameFrontend):
        def execution_configuration(self, args):
            return profile_configuration(
                args.profile,
                program_identity=self.program_identity(args),
                provider_preference=self.default_provider_preference,
                bootstrap_provider=NativeBootstrapProvider(
                    "test-native",
                    ("test state",),
                    provider_digest="test-native-v1",
                ),
            )

        def execution_coverage(self, args):
            return ProgramCoverage(
                roots=("root",),
                reachable=frozenset({"root", "frame"}),
                evidence_identity="tiny-coverage",
            )

        def execution_implementations(self, args):
            return ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
                implementation_id="mixed-external",
                targets=frozenset({"root", "frame"}),
                origin=ImplementationOrigin.GENERATED,
                properties=frozenset({"vmless", "dos-memory-backed"}),
                implementation_digest="tiny-v1",
            )),))

        default_provider_preference = ("mixed-external",)

    args = _parse(Fe(ROOT), ["--profile", "detached"])
    plan = Fe(ROOT).resolve_execution_plan(args)
    assert plan.report.is_detached_from("original-exe")
    assert {binding.implementation_id for binding in plan.bindings} == {
        "mixed-external"
    }


def test_detached_launch_warns_about_static_frontier_without_blocking(capsys):
    class Fe(GameFrontend):
        def execution_configuration(self, args):
            return profile_configuration(
                args.profile,
                program_identity="test:detached-warning",
                provider_preference=("generated",),
                bootstrap_provider=NativeBootstrapProvider(
                    "test-native", ("test state",),
                    provider_digest="test-native-v1",
                ),
            )

        def execution_coverage(self, args):
            return ProgramCoverage(
                roots=("root",),
                reachable=frozenset({"root", "point:dispatch"}),
                unresolved_edges=(
                    "root --call_ind--> point:dispatch",
                ),
                evidence_identity="uncertain-test",
            )

        def execution_implementations(self, args):
            return ImplementationCatalog((ImplementationEntry(
                ImplementationDescriptor(
                    "generated",
                    frozenset({"root", "point:dispatch"}),
                    ImplementationOrigin.GENERATED,
                    implementation_digest="generated-v1",
                ),
            ),))

        def launch(self, args, plan):
            assert plan.configuration.execution_policy.fallback.value == "forbidden"
            return 0

    assert player.main(Fe(ROOT), ["--profile", "detached"]) == 0
    error = capsys.readouterr().err
    assert "static closure uncertainty" in error
    assert "unknown indirect targets: 1" in error
    assert "point:dispatch" not in error


def test_release_cannot_override_strict_closure(capsys):
    assert player.main(GameFrontend(ROOT), [
        "--profile", "release", "--closure-policy", "permissive",
        "--plan-only",
    ]) == 2
    assert "release profile requires strict static closure" in (
        capsys.readouterr().err
    )


def test_plan_only_reports_without_runtime_construction(capsys):
    class Fe(GameFrontend):
        def create_runtime(self, args):
            raise AssertionError("--plan-only must not construct a runtime")

    assert player.main(
        Fe(ROOT), ["--exe", str(Path(__file__)), "--plan-only"]
    ) == 0
    output = capsys.readouterr().out
    assert "execution profile: development" in output
    assert "original-exe detached: false" in output
    assert "plan digest:" in output


def test_plan_only_uses_frontend_diagnostics_without_replanning(capsys):
    class Fe(GameFrontend):
        def format_execution_plan(self, args, plan):
            return super().format_execution_plan(args, plan) + "\nproduct role: shell"

        def create_runtime(self, args):
            raise AssertionError("--plan-only must not construct a runtime")

    assert player.main(
        Fe(ROOT), ["--exe", str(Path(__file__)), "--plan-only"]
    ) == 0
    assert "product role: shell" in capsys.readouterr().out


def test_run_headless_respects_frame_budget(capsys):
    class Fe(GameFrontend):
        name = "stub"

        def advance_frame(self, rt, args, frame):
            rt.cpu.instruction_count += 10

    class CPU(_StubCPU):
        def __init__(self):
            super().__init__()
            self.instruction_count = 0
            self.s = type("S", (), {"snapshot": staticmethod(lambda: "stub-cpu-state")})()

    rt = _StubRuntime()
    rt.cpu = CPU()
    fe = Fe(ROOT)
    args = _parse(fe, ["--headless", "--frames", "7"])
    assert player.run_headless(fe, rt, args) == 0
    assert rt.cpu.instruction_count == 70
    assert "frames: 7" in capsys.readouterr().out


def test_verification_profile_requires_an_explicit_interval():
    with pytest.raises(SystemExit, match="requires --play-replay"):
        player.main(GameFrontend(ROOT), [
            "--exe", str(Path(__file__)), "--profile", "verification",
        ])


def test_verification_profile_dispatches_before_runtime_boot(monkeypatch):
    class Fe(GameFrontend):
        def create_runtime(self, args):
            raise AssertionError("verification must use replay drivers")

    seen = []
    monkeypatch.setattr(
        player,
        "_run_differential_verification",
        lambda frontend, args, plan: seen.append(plan) or 7,
    )
    assert player.main(Fe(ROOT), [
        "--exe", str(Path(__file__)), "--profile", "verification",
    ]) == 7
    assert seen[0].configuration.verification_policy.mode == "differential"


def test_differential_verification_restores_recorded_pacing_before_drivers(
    monkeypatch,
):
    artifact = SimpleNamespace(
        metadata={"steps_per_frame": 123},
        timeline_id="timeline-v1",
    )
    monkeypatch.setattr(
        player.ReplayArtifact, "open", lambda path: artifact)
    monkeypatch.setattr(
        player,
        "verify_interval",
        lambda *args: SimpleNamespace(
            equivalent=True,
            comparison=SimpleNamespace(oracle_digest="digest"),
        ),
    )

    class Fe:
        def apply_replay_metadata(self, args, metadata):
            args.steps_per_frame = metadata["steps_per_frame"]

        def verification_drivers(self, args, plan, opened):
            assert opened is artifact
            assert args.steps_per_frame == 123
            return object(), object()

    args = SimpleNamespace(
        play_replay="replay",
        verify_start=0,
        verify_end=1,
        bisect=False,
        steps_per_frame=1,
    )
    assert player._run_differential_verification(Fe(), args, object()) == 0


def test_selected_implementation_activator_is_the_only_binding_authority():
    activated = []

    class Fe(GameFrontend):
        default_provider_preference = ("native-frame",)

        def execution_configuration(self, args):
            return profile_configuration(
                args.profile,
                program_identity=self.program_identity(args),
                selected_overrides=("native-frame",),
                provider_preference=self.default_provider_preference,
            )

        def execution_coverage(self, args):
            return ProgramCoverage(
                roots=("frame",),
                reachable=frozenset({"frame"}),
                evidence_identity="frame-v1",
            )

        def execution_implementations(self, args):
            descriptor = ImplementationDescriptor(
                implementation_id="native-frame",
                targets=frozenset({"frame"}),
                origin=ImplementationOrigin.AUTHORED,
                category=OverrideCategory.FAITHFUL,
                implementation_digest="native-v1",
            )
            return ImplementationCatalog((ImplementationEntry(
                descriptor,
                implementation=lambda: None,
                adapters=(BackendAdapter(
                    "native-frame/interpreted",
                    INTERPRETED_CPU_CARRIER,
                    lambda runtime, targets: activated.append((runtime, targets)),
                    "native-frame-adapter-v1",
                ),),
            ),))

    frontend = Fe(ROOT)
    args = _parse(frontend, [])
    plan = frontend.resolve_execution_plan(args)
    runtime = SimpleNamespace()
    frontend.bind_execution_plan(runtime, plan)
    assert activated == [(runtime, ("frame",))]


def test_selected_implementation_never_silently_falls_back_on_wrong_backend():
    class Fe(GameFrontend):
        default_provider_preference = ("native-frame",)

        def execution_configuration(self, args):
            return profile_configuration(
                args.profile,
                program_identity=self.program_identity(args),
                selected_overrides=("native-frame",),
                provider_preference=self.default_provider_preference,
            )

        def execution_coverage(self, args):
            return ProgramCoverage(
                roots=("frame",), reachable=frozenset({"frame"}),
                evidence_identity="frame-v1",
            )

        def execution_implementations(self, args):
            descriptor = ImplementationDescriptor(
                implementation_id="native-frame",
                targets=frozenset({"frame"}),
                origin=ImplementationOrigin.AUTHORED,
                category=OverrideCategory.FAITHFUL,
                implementation_digest="native-v1",
            )
            return ImplementationCatalog((ImplementationEntry(
                descriptor,
                implementation=lambda: None,
                adapters=(BackendAdapter(
                    "native-frame/interpreted",
                    INTERPRETED_CPU_CARRIER,
                    lambda *_: None,
                    "native-frame-adapter-v1",
                ),),
            ),))

    frontend = Fe(ROOT)
    plan = frontend.resolve_execution_plan(_parse(frontend, []))
    with pytest.raises(RuntimeError, match="no adapter for carrier 'generated-cpuless'"):
        frontend.bind_execution_plan(
            SimpleNamespace(), plan, carrier_id=GENERATED_CPULESS_CARRIER
        )


def test_selected_enhancement_activates_at_its_seam_without_owning_it():
    activated = []

    class Fe(GameFrontend):
        def execution_configuration(self, args):
            return profile_configuration(
                args.profile,
                program_identity=self.program_identity(args),
                selected_overrides=("wide-presenter",),
                provider_preference=("generated-frame",),
            )

        def execution_coverage(self, args):
            return ProgramCoverage(
                roots=("frame",), reachable=frozenset({"frame"}),
                evidence_identity="frame-v1",
            )

        def execution_implementations(self, args):
            entries = []
            for implementation_id, origin, category in (
                ("generated-frame", ImplementationOrigin.GENERATED,
                 OverrideCategory.BASELINE),
                ("wide-presenter", ImplementationOrigin.AUTHORED,
                 OverrideCategory.ENHANCEMENT),
            ):
                descriptor = ImplementationDescriptor(
                    implementation_id=implementation_id,
                    targets=frozenset({"frame"}),
                    origin=origin,
                    category=category,
                    implementation_digest=implementation_id,
                )
                entries.append(ImplementationEntry(
                    descriptor,
                    adapters=(BackendAdapter(
                        f"{implementation_id}/interpreted",
                        INTERPRETED_CPU_CARRIER,
                        lambda runtime, targets, name=implementation_id:
                        activated.append((name, runtime, targets)),
                        implementation_id + "-adapter-v1",
                    ),),
                ))
            return ImplementationCatalog(tuple(entries))

    frontend = Fe(ROOT)
    args = _parse(frontend, [])
    plan = frontend.resolve_execution_plan(args)
    assert [(item.target, item.implementation_id) for item in plan.bindings] == [
        ("frame", "generated-frame"),
    ]
    runtime = SimpleNamespace()
    frontend.bind_execution_plan(runtime, plan)
    assert activated == [
        ("generated-frame", runtime, ("frame",)),
        ("wide-presenter", runtime, ("frame",)),
    ]


def test_runtime_diagnostics_expose_bound_plan_and_region_lifecycle():
    runtime = SimpleNamespace(
        execution_plan=SimpleNamespace(
            plan_digest="a" * 64,
            report=SimpleNamespace(execution_carrier="interpreted-cpu"),
        ),
        execution_carrier_id="interpreted-cpu",
    )
    runtime.execution_regions = SimpleNamespace(
        active_region_id="native-gameplay",
        last_region_id="native-gameplay",
        last_entry_id="start-level",
        last_exit_id="",
    )

    lines = player._diagnostic_lines(runtime)

    assert lines[0].startswith("execution: carrier=interpreted-cpu plan=")
    assert lines[1] == (
        "execution region: active=native-gameplay last=native-gameplay "
        "entry=start-level exit=none"
    )
