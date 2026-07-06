"""TEMPLATE — game adapter runtime wiring.

Boot a fresh runtime for your game and load snapshots back.  Everything here is
game knowledge: the EXE path/name, the command tail (video/sound selection),
bootstrap accelerators (packer loops), and which replacement hooks to install.

TODO for a new game: replace every placeholder below after deriving the real
values from your oracle (see docs/porting_new_game.md).
"""
from __future__ import annotations

from pathlib import Path

from dos_re.runtime import Runtime, create_runtime
from dos_re.snapshot import load_snapshot

# TODO: your game's executable name (and a resolver if it ships packed).
EXE_NAME = "MYGAME.EXE"


def create_game_runtime(
    exe_path: str | Path,
    *,
    game_root: str | Path | None = None,
    command_tail: bytes | str = b"",
    install_replacements: bool = True,
) -> Runtime:
    """Boot a fresh runtime.  ``install_replacements=False`` is the pure-ASM
    oracle: no recovered hooks, the CPU runs the original code verbatim."""
    if install_replacements:
        # Importing the hooks module registers all @registry.replace handlers;
        # dos_re.runtime.create_runtime installs the registry into the CPU.
        from . import hooks  # noqa: F401
    return create_runtime(exe_path, game_root=game_root, command_tail=command_tail)


def load_game_snapshot(
    exe_path: str | Path,
    snapshot_dir: str | Path,
    *,
    game_root: str | Path | None = None,
) -> Runtime:
    rt = load_snapshot(exe_path, snapshot_dir, game_root=game_root)
    # TODO: game-specific snapshot repairs go here (see overkill_port's
    # repair_object_allocator_cursor for a real example of why this exists).
    return rt
