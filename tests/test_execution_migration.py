"""Guard the one-player dos_re 3.0 execution lifecycle."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import dos_re.hooks as hooks
import dos_re.pm_player as pm_player

from dos_re.player import build_arg_parser


ROOT = Path(__file__).resolve().parents[1]


def test_parallel_execution_authorities_are_gone():
    assert not hasattr(hooks, "HookRegistry")
    assert not hasattr(hooks, "registry")
    assert not hasattr(pm_player, "main")
    for relative in (
        "tools/display.py",
        "tools/pm_view.py",
        "tools/replay_verify.py",
        "tools/audit_hook_oracle.py",
    ):
        assert not (ROOT / relative).exists()


def test_protected_mode_uses_the_canonical_parser():
    frontend = pm_player.PMFrontend(ROOT)
    args = build_arg_parser(frontend).parse_args(
        ["--exe", "GAME.EXE", "--png", "frame.png", "--no-sound"]
    )
    assert args.profile == "development"
    assert args.png == "frame.png"
    assert args.no_sound


def test_new_project_scaffolds_exactly_one_player(tmp_path: Path):
    project = tmp_path / "game"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "new_project.py"),
            "--game",
            "game",
            "--output",
            str(project),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    players = tuple((project / "scripts").glob("play*.py"))
    assert tuple(path.name for path in players) == ("play.py",)
