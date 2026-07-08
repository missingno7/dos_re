"""Headless smoke test for the generic live oracle viewer (tools/view.py).

Skips when the optional viewer deps (numpy + pygame) are absent — CI installs
only pytest, so this runs on dev machines with the viewer extras installed.
Uses SDL's dummy video driver: no window, real code path."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pygame")

ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_DIR = ROOT / "examples" / "tiny_frame_game"
if not _EXAMPLE_DIR.is_dir():
    pytest.skip("examples/tiny_frame_game removed — viewer smoke test needs its EXE builder",
                allow_module_level=True)
sys.path.insert(0, str(_EXAMPLE_DIR))

from game import build_game_exe  # noqa: E402


def test_viewer_presents_frames_headless(tmp_path):
    exe = build_game_exe(tmp_path / "TINY.EXE")
    env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "view.py"), "--exe", str(exe),
         "--frames", "5", "--steps-per-frame", "3000", "--present-hz", "250"],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "frames: 5" in result.stdout
