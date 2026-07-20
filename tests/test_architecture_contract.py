"""Repository guards for the single dos_re 3.0 authority graph."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dos_re.player import GameFrontend, build_arg_parser

ROOT = Path(__file__).resolve().parents[1]

LEGACY_PATHS = (
    "dos_re/input_demo.py",
    "dos_re/pm_input_demo.py",
    "dos_re/pm_player.py",
    "dos_re/coverage.py",
    "dos_re/hook_taxonomy.py",
    "dos_re/islands.py",
    "dos_re/frontier.py",
    "dos_re/gaps.py",
    "dos_re/boundary_clock.py",
    "dos_re/checkpoints.py",
    "dos_re/lift/shadow.py",
    "dos_re/lift/standalone.py",
    "tools/gen_island_manifest.py",
)
LEGACY_CURRENT_TOKENS = (
    "dos_re.input_demo",
    "dos_re.pm_input_demo",
    "dos_re.pm_player",
    "dos_re.coverage",
    "dos_re.hook_taxonomy",
    "dos_re.islands",
    "dos_re.frontier",
    "dos_re.gaps",
    "dos_re.boundary_clock",
    "dos_re.checkpoints",
    "dos_re.lift.shadow",
    "dos_re.lift.standalone",
    "ExecutionProfile",
    "install_vmless_graph",
    "install_passing_lifts",
    "--install-passing",
    "--keep-interpreted",
    "--require-vmless-wall",
    "--structural-edges",
    "--proven-edges",
    "--overrides",
    "--record-demo",
    "--play-demo",
    "--demo-continue",
    "--demo-dir",
    "play_vmless",
    "play_cpuless",
    "play_native",
    "play_memoryless",
    "tick_demo",
    "frontend_timeline",
)


def _imports(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "" if node.level == 0 else "dos_re."
            result.add(prefix + (node.module or ""))
    return result


def test_removed_parallel_authorities_do_not_exist() -> None:
    assert not [path for path in LEGACY_PATHS if (ROOT / path).exists()]


def test_active_source_and_documentation_have_no_legacy_current_api() -> None:
    roots = ("dos_re", "docs", "examples", "tools")
    files = [
        path
        for root in roots
        for path in (ROOT / root).rglob("*")
        if path.suffix in {".py", ".md", ".toml"}
        and "history" not in path.parts
    ]
    files.extend((ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "pyproject.toml"))
    offenders = {
        str(path.relative_to(ROOT)): token
        for path in files
        for token in LEGACY_CURRENT_TOKENS
        if token in path.read_text(encoding="utf-8")
    }
    assert not offenders


def test_authority_layers_remain_acyclic() -> None:
    identity_forbidden = {
        "dos_re.replay", "dos_re.atlas", "dos_re.execution", "dos_re.player",
    }
    replay_forbidden = {"dos_re.atlas", "dos_re.execution", "dos_re.player"}
    atlas_forbidden = {
        "dos_re.player", "dos_re.cpu", "dos_re.cpu386", "dos_re.pm_backend",
    }
    execution_forbidden = {
        "dos_re.atlas", "dos_re.replay", "dos_re.player", "dos_re.cpu",
        "dos_re.cpu386", "dos_re.pm_backend",
    }
    assert not (_imports("dos_re/identity.py") & identity_forbidden)
    assert not (_imports("dos_re/replay.py") & replay_forbidden)
    assert not (_imports("dos_re/atlas.py") & atlas_forbidden)
    assert not (_imports("dos_re/execution.py") & execution_forbidden)


def test_unified_player_exposes_only_replay_cli() -> None:
    parser = build_arg_parser(GameFrontend(ROOT))
    args = parser.parse_args([
        "--record-replay", "recorded",
        "--play-replay", "played",
        "--replay-dir", "replays",
    ])
    assert args.record_replay == "recorded"
    assert args.play_replay == "played"
    assert args.replay_dir == "replays"
    with pytest.raises(SystemExit):
        parser.parse_args(["--play-demo", "legacy"])
