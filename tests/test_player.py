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

import pytest

from dos_re import player
from dos_re.execution import (
    NativeBootstrapProvider,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    ProgramCoverage,
    profile_configuration,
)
from dos_re.player import GameFrontend, build_arg_parser

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


def test_standard_cli_defaults():
    fe = GameFrontend(ROOT)
    args = _parse(fe, [])
    assert args.headless is False           # viewer is the default state
    assert args.profile == "development"
    assert args.plan_only is False
    assert args.play_demo is None
    assert args.demo_continue is False
    assert args.steps_per_frame == GameFrontend.default_steps_per_frame
    assert args.timer_irqs_per_frame == GameFrontend.default_timer_irqs_per_frame
    assert args.present_hz == GameFrontend.default_present_hz
    assert args.snapshot is None and args.save_snapshot is None
    assert args.dos_args == ""


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


def test_demo_metadata_roundtrip():
    fe = GameFrontend(ROOT)
    args = _parse(fe, ["--exe", "GAME.EXE", "--steps-per-frame", "555",
                       "--timer-irqs-per-frame", "3"])
    meta = fe.demo_metadata(args)
    assert meta["steps_per_frame"] == 555 and meta["timer_irqs_per_frame"] == 3

    fresh = _parse(fe, ["--exe", "GAME.EXE"])   # defaults, as a replay would start
    fe.apply_demo_metadata(fresh, meta)
    assert fresh.steps_per_frame == 555 and fresh.timer_irqs_per_frame == 3


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
    with pytest.raises(SystemExit, match="requires --play-demo"):
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
                implementation_digest="native-v1",
            )
            return ImplementationCatalog((ImplementationEntry(
                descriptor,
                implementation=lambda: None,
                activate=lambda runtime, targets: activated.append(
                    (runtime, targets)
                ),
            ),))

    frontend = Fe(ROOT)
    args = _parse(frontend, [])
    plan = frontend.resolve_execution_plan(args)
    runtime = object()
    frontend.bind_execution_plan(runtime, plan)
    assert activated == [(runtime, ("frame",))]
