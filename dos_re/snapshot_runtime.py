"""Build an EXE-free VM runtime from a persisted snapshot.

This module intentionally owns the CPU/runtime-shell dependency. The reusable
DOS and device-state codec lives in :mod:`dos_re.snapshot_headless` and remains
safe to package with CPUless products.
"""
from __future__ import annotations

import json
from pathlib import Path

from .snapshot_headless import _restore_dos_state


def load_snapshot_headless(
    snapshot_dir: str | Path,
    *,
    game_root: str | Path,
    memory_name: str = "memory_1mb.bin",
):
    """Restore a snapshot without loading or parsing the original executable."""
    from .cpu import CPUState
    from .runtime_core import create_runtime_from_image

    snap = Path(snapshot_dir)
    meta = json.loads((snap / "state.json").read_text(encoding="utf-8"))
    memory_image = (snap / memory_name).read_bytes()
    state = CPUState(**meta["cpu"])
    runtime = create_runtime_from_image(
        memory_image,
        state,
        game_root=game_root,
        psp_segment=meta.get("program", {}).get("psp_segment", 0),
        program_meta=meta.get("program"),
    )
    _restore_dos_state(runtime, meta.get("dos", {}))
    # PIT phase and other time-derived device state depend on this counter.
    runtime.cpu.instruction_count = int(meta.get("steps") or 0)
    return runtime
