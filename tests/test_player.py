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
from dos_re.hooks import registry as hook_registry
from dos_re.player import GameFrontend, HookModeUnsupported, build_arg_parser

ROOT = Path(__file__).resolve().parents[1]


def _parse(frontend: GameFrontend, argv: list[str]):
    return build_arg_parser(frontend).parse_args(argv)


def test_import_stays_lazy_about_viewer_deps():
    """Importing dos_re.player must not pull in numpy or pygame — headless demo
    replay has to work on machines without the viewer extras."""
    code = ("import sys; import dos_re.player; "
            "bad = [m for m in ('numpy', 'pygame') if m in sys.modules]; "
            "assert not bad, f'dos_re.player eagerly imported {bad}'")
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


def test_standard_cli_defaults():
    fe = GameFrontend(ROOT)
    args = _parse(fe, [])
    assert args.headless is False           # viewer is the default state
    assert args.play_demo is None
    assert args.demo_continue is False
    assert args.no_replacements is False
    assert args.safe_hooks is False and args.verify_hooks is False and args.trace_hooks is False
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


def test_demo_acquisition_and_runtime_loading_are_frontend_seams(tmp_path):
    class Fe(GameFrontend):
        def create_runtime(self, args):
            return "cold"

        def load_snapshot_runtime(self, args, snapshot_dir):
            return ("snapshot", snapshot_dir)

    fe = Fe(ROOT)
    recorder = fe.create_demo_recorder(root=tmp_path, name="x", metadata={})
    assert recorder.name == "x"
    cold = type("Playback", (), {"is_cold_start": True})()
    snap = type("Playback", (), {
        "is_cold_start": False,
        "snapshot_path": lambda self: tmp_path / "snap",
    })()
    assert fe.load_demo_runtime(object(), cold) == "cold"
    assert fe.load_demo_runtime(object(), snap) == ("snapshot", tmp_path / "snap")


class _StubCPU:
    def __init__(self):
        self.replacement_hooks = {}
        self.hook_names = {}


class _StubRuntime:
    def __init__(self):
        self.cpu = _StubCPU()


def test_no_replacements_uninstalls_registry_hooks_only():
    key = (0x7777, 0x0001)   # unlikely to collide with a real registration
    assert key not in hook_registry.replacements
    hook_registry.replacements[key] = object()
    try:
        rt = _StubRuntime()
        framework_key = (0xF000, 0xE987)                 # BIOS INT9 stays installed
        rt.cpu.replacement_hooks = {key: "game_hook", framework_key: "bios_int9"}
        rt.cpu.hook_names = {key: "game_hook", framework_key: "bios_int9"}
        fe = GameFrontend(ROOT)
        args = _parse(fe, ["--no-replacements"])
        fe.apply_hook_mode(rt, args)
        assert key not in rt.cpu.replacement_hooks
        assert framework_key in rt.cpu.replacement_hooks
    finally:
        del hook_registry.replacements[key]


@pytest.mark.parametrize("flag", ["--safe-hooks", "--verify-hooks", "--trace-hooks"])
def test_unimplemented_hook_tiers_fail_loud(flag):
    fe = GameFrontend(ROOT)
    args = _parse(fe, [flag])
    with pytest.raises(HookModeUnsupported):
        fe.apply_hook_mode(_StubRuntime(), args)


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
