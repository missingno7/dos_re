"""Watch the oracle run — the generic interactive viewer for any dos_re runtime.

This command uses ``dos_re.player`` directly. The default
:class:`~dos_re.player.GameFrontend` boots any EXE with the
generic runtime and the simple deterministic pacing model, so you get the full
standard CLI — viewer by default (``--headless`` to disable), snapshot
save/resume, demo record/replay (F11), F12 snapshots, F10 screenshots — with no
game adapter at all. A game project's single ``scripts/play.py`` subclasses
GameFrontend instead.

Usage:
    python tools/view.py --exe assets/GAME.EXE [--dos-args "..."]
    python tools/view.py --protected-mode --exe assets/GAME.EXE
                         [--steps-per-frame 40000] [--timer-irqs-per-frame 0]
                         [--snapshot DIR] [--frames N] [--square-pixels]

--timer-irqs-per-frame delivers INT 08h that many times per host frame for
games that advance on the PIT ISR (leave 0 for retrace-paced games).
--frames N exits after N presented frames — headless smoke use with
SDL_VIDEODRIVER=dummy. The viewer needs numpy + pygame (the framework core
does not).

"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.player import GameFrontend, main as player_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    protected = "--protected-mode" in arguments
    if protected:
        arguments.remove("--protected-mode")
        from dos_re.pm_player import PMFrontend
        frontend = PMFrontend(ROOT)
    else:
        frontend = GameFrontend(ROOT)
    return player_main(frontend, arguments, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
