"""The tiny_frame_game walkthrough doubles as an end-to-end integration test:
it is the only place the whole stack (boot -> checkpoints -> INT9 input ->
cold-start demos -> snapshots -> hook oracle -> frame oracle -> state mirror)
runs against a live runtime inside this repo.

The examples are optional material (see examples/README.md): if the examples/
directory is removed, these tests skip and the framework suite stays green."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_DIR = ROOT / "examples" / "tiny_frame_game"
if not _EXAMPLE_DIR.is_dir():
    pytest.skip("examples/tiny_frame_game removed — example tests are optional",
                allow_module_level=True)
sys.path.insert(0, str(_EXAMPLE_DIR))

import walkthrough  # noqa: E402
from game import build_game_exe  # noqa: E402


def test_oracle_boot_and_frames(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    rows = walkthrough.stage_oracle(exe)
    assert [r[0] for r in rows] == [0, 1, 2, 3]


def test_cold_start_demo_record_replay_roundtrip(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.stage_cold_start_demo(exe, tmp_path)


def test_snapshot_restore_equivalence(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.stage_snapshot(exe, tmp_path)


def test_hook_oracle_catches_wrong_and_verifies_correct(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.stage_hooks(exe)


def test_frame_verifier_lockstep_and_divergence(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.stage_frame_verifier(exe, tmp_path)


def test_state_mirror_views(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.stage_state_mirror(exe)
