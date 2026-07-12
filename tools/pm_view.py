"""Watch any DOS/4GW (MZ+LE) executable run, zero setup — the PM view.py.

The protected-mode counterpart of ``tools/view.py``: a live pygame window
over the flat 386 runtime with keyboard (8042 KBC scancodes), mouse
(INT 33h), wall-clock vsync pacing, F10 screenshot and F12 snapshot — no
adapter required.  A port's own ``scripts/play.py`` (a thin wrapper over
``dos_re.pm_player.main``) supersedes this once the adapter exists.

Usage:
    python tools/pm_view.py --exe assets/GAME.EXE [--scale 3]
    python tools/pm_view.py --exe assets/GAME.EXE --headless --steps N [--png f.png]
    python tools/pm_view.py --exe assets/GAME.EXE --snapshot <dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.pm_player import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(description=__doc__.splitlines()[0]))
