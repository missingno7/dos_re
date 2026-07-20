"""The tiny_frame_game capability demonstrations double as integration tests.

Together they cover identity, retained IR, replay, Atlas queries, coverage,
planning, detachment, continuation, and verification without defining a
required recovery order.

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
    rows = walkthrough.demonstrate_oracle(exe)
    assert [r[0] for r in rows] == [0, 1, 2, 3]


def test_replay_artifact_record_replay_roundtrip(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    _, _, function_id = walkthrough.stable_program_identity(exe)
    walkthrough.demonstrate_replay_artifact(exe, tmp_path, function_id)


def test_snapshot_restore_equivalence(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.demonstrate_snapshot(exe, tmp_path)


def test_hook_oracle_catches_wrong_and_verifies_correct(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    program, image, function_id = walkthrough.stable_program_identity(exe)
    artifact = walkthrough.demonstrate_replay_artifact(exe, tmp_path, function_id)
    coverage = walkthrough.demonstrate_atlas_and_planning(
        exe, tmp_path, artifact, program, image, function_id)
    walkthrough.demonstrate_focused_verification(exe, function_id, coverage)


def test_frame_verifier_lockstep_and_divergence(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    program, image, function_id = walkthrough.stable_program_identity(exe)
    artifact = walkthrough.demonstrate_replay_artifact(exe, tmp_path, function_id)
    coverage = walkthrough.demonstrate_atlas_and_planning(
        exe, tmp_path, artifact, program, image, function_id)
    walkthrough.demonstrate_frame_verifier(
        exe, tmp_path, function_id, coverage)


def test_state_mirror_views(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    walkthrough.demonstrate_state_view(exe)
