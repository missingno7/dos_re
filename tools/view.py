"""Watch the oracle run — the generic interactive viewer for any dos_re runtime.

This is now a thin wrapper over ``dos_re.player`` (the game-agnostic play-runner
core): the default :class:`~dos_re.player.GameFrontend` boots any EXE with the
generic runtime and the simple deterministic pacing model, so you get the full
standard CLI — viewer by default (``--headless`` to disable), snapshot
save/resume, demo record/replay (F11), F12 snapshots, F10 screenshots — with no
game adapter at all.  A real port's ``scripts/play.py`` subclasses GameFrontend
instead (worked example: the Lemmings pilot's lemmings_port/scripts/ runners).

Usage:
    python tools/view.py --exe assets/GAME.EXE [--dos-args "..."]
                         [--steps-per-frame 40000] [--timer-irqs-per-frame 0]
                         [--snapshot DIR] [--frames N] [--square-pixels]

--timer-irqs-per-frame delivers INT 08h that many times per host frame for
games that advance on the PIT ISR (leave 0 for retrace-paced games).
--frames N exits after N presented frames — headless smoke use with
SDL_VIDEODRIVER=dummy. The viewer needs numpy + pygame (the framework core
does not).

The decode helpers previous copies of this file exported are re-exported so
existing ``import view; view.decode_frame(rt)`` users keep working.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.player import (  # noqa: E402,F401 — re-exports are this shim's API
    HEIGHT,
    WIDTH,
    GameFrontend,
    decode_frame_default as decode_frame,
    main as player_main,
    scancode_table as _scancode_table,
)


def main(argv: list[str] | None = None) -> int:
    return player_main(GameFrontend(ROOT), argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
