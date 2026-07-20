"""Scaffold a minimal dos_re 3.0 game recovery project.

The generated project consumes the checked-out ``dos_re`` framework and
establishes:

    project configuration + .gitignore (originals never committed)
    assets/ as recovery-time input only
    <game>/recovery_facts/ (evidence-backed project facts)
    <game>/lifted/ for optional generated implementations
    artifacts/ (replays) + generated/ (boot image), both git-ignored
    scripts/play.py (the one profile-driven development/release entrypoint)
    tests/ with the single-player authority guard

Usage:
    python dos_re/tools/new_project.py --game mygame --output ../mygame_port \
        [--exe MYGAME.EXE]

After generating: add dos_re as a git submodule at <output>/dos_re, put your
original game files in assets/, and follow docs/getting_started.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        raise SystemExit(f"refusing to overwrite existing file: {p}")
    p.write_text(text, encoding="utf-8", newline="\n")
    print(f"  {rel}")


def scaffold(game: str, out: Path, exe_name: str) -> None:
    g = game.lower()
    if not g.isidentifier():
        raise SystemExit(f"--game must be a valid Python identifier: {game!r}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"scaffolding dos_re 3.0 port {g!r} in {out}")

    _w(out, "README.md", f"""# {g}_port -- a dos_re 3.0 recovery project

Started with `dos_re/tools/new_project.py`.

- Execution architecture: `dos_re/docs/execution_planner.md`
- Override architecture: `dos_re/docs/override_architecture.md`
- Composable operations: `dos_re/docs/getting_started.md`
- Evidence and navigation: `dos_re/docs/execution_atlas.md`

Bring your own original game files into `assets/` (git-ignored). The binary is
development-time input only; a release plan and export must not load it.
""")

    _w(out, ".gitignore", """# Original game files never belong in this repo (bring your own)
assets/

# Replays/snapshots carry original-game memory: local only
artifacts/

# Generated data-only boot image: derived from the original binary, local only
generated/

__pycache__/
*.pyc
""")

    _w(out, "AGENTS.md", f"""# AGENTS.md -- operating card ({g}, dos_re 3.0)

Start with `dos_re/README.md`, `dos_re/docs/architecture.md`, and
`dos_re/docs/getting_started.md`. Use only the observation, analysis,
generation, replay, and rewriting operations that provide value for this game.
Record evidence-backed facts in `{g}/recovery_facts/` and never hand-edit
generated output. `scripts/play.py` is the only player. Representation
properties belong to implementation catalog entries, not runner names or
project stages.
""")

    _w(out, f"{g}/__init__.py", "")
    _w(out, f"{g}/runtime.py", f'''"""{g} adapter runtime wiring (the game-specific boot knowledge).

Everything here is game knowledge: the EXE identity, boot-time inputs, and
game-specific interrupt quirks that must NOT live in the game-agnostic
framework (dos_re).  This module is the ONLY place that loads the original
executable -- and only for development/oracle paths, never for a detached plan.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.runtime import Runtime, create_runtime

EXE_NAME = "{exe_name}"


def create_{g}_runtime(exe_path: str | Path, *, game_root: str | Path | None = None,
                       command_tail: bytes | str = b"") -> Runtime:
    """Boot the interpreted oracle for the original executable.

    Add boot-time input feeding / interrupt quirks here as you discover them
    and record their evidence in this project."""
    return create_runtime(exe_path, game_root=game_root, command_tail=command_tail)
''')

    _w(out, f"{g}/facts.py", f'''"""Load {g}'s explicit identity-based recovery facts.

Game-specific claims live in versioned declarations with provenance. Optional
analyses, generators, and Atlas ingestion may cite them; generated output never
becomes their source of truth.
"""
from __future__ import annotations

import json
from pathlib import Path

_FACTS_PATH = (Path(__file__).resolve().parent / "recovery_facts"
               / "recovery_facts.json")


def load_facts() -> dict:
    return json.loads(_FACTS_PATH.read_text(encoding="utf-8"))


def environment_wait_entries() -> set[str]:
    """Entries proven to perform an environment-wait effect."""
    return {{rec["entry"].upper() for rec in load_facts().get("environment_waits", ())}}


def code_as_data_ranges() -> list[tuple[int, int]]:
    """Declared code_as_data ranges as (linear_start, length) -- recovered code
    bytes the game READS AS DATA; the boot-image poison must preserve them."""
    out = []
    for rec in load_facts().get("code_as_data", ()):
        lin = rec["linear"]
        start = int(lin, 16) if isinstance(lin, str) else int(lin)
        out.append((start, int(rec["length"])))
    return out
''')

    _w(out, f"{g}/recovery_facts/recovery_facts.json", """{
 "$schema_note": "Structured, evidence-backed, game-specific recovery facts. Analyses and generators consume these; generated output never encodes them by hand-edit. Every fact carries provenance (why + evidence + recorded date).",
 "game": "GAME_NAME",
 "environment_waits": [],
 "code_as_data": [],
 "notes": []
}
""".replace("GAME_NAME", g))

    _w(out, f"{g}/lifted/README.md", f"""# `{g}/lifted/` -- optional generated implementations

**AUTOGENERATED -- DO NOT HAND EDIT.**

This directory may contain literal, CPUless, or ABI-recovered implementations
produced from retained Recovery IR or a targeted scan. Generation is optional,
and neighboring functions may use different representations. Every generated
artifact must retain its source/toolchain provenance and enter the project's
ImplementationCatalog before an ExecutionPlan can select it.

See `dos_re/docs/lifting_design.md`.
""")

    _w(out, "scripts/play.py", f'''"""The single {g} execution entrypoint.

Representation depth is an implementation property. Select development,
verification, detached, or release policy with ``--profile``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dos_re"))

from dos_re import player  # noqa: E402

from {g}.runtime import create_{g}_runtime, EXE_NAME  # noqa: E402


class GameFrontend(player.GameFrontend):
    name = "{g}"
    default_exe = str(ROOT / "assets" / EXE_NAME)
    default_game_root = str(ROOT / "assets")

    def create_runtime(self, args):
        return create_{g}_runtime(args.exe, game_root=args.game_root,
                                  command_tail=args.dos_args)


def main(argv=None) -> int:
    return player.main(GameFrontend(ROOT), argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
''')

    _w(out, "tests/test_architecture.py", f'''"""Project-level dos_re 3.0 authority guard."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_has_one_player_entrypoint():
    assert tuple(path.name for path in (ROOT / "scripts").glob("play*.py")) == ("play.py",)
''')

    print("\nnext steps:")
    print(f"  1. cd {out} && git init && git submodule add <dos_re repo url> dos_re")
    print(f"  2. put your original game files into assets/ (incl. {exe_name})")
    print("  3. follow dos_re/docs/getting_started.md (make scripts/play.py boot)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", required=True,
                    help="short package name for the port (python identifier)")
    ap.add_argument("--output", required=True, help="directory to create")
    ap.add_argument("--exe", default=None,
                    help="original executable filename (default GAME.EXE)")
    args = ap.parse_args(argv)
    exe = args.exe or f"{args.game.upper()}.EXE"
    scaffold(args.game, Path(args.output), exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
