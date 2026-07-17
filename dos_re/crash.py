"""Save the machine at the moment it broke, so the next step is a debugger and
not a re-run.

A recovery session's failures arrive deep: a wall violation 1,100 frames into a
cold boot, an iteration guard at frame 280, a palette that goes wrong once in
1,832 frames. The state that explains each one exists exactly when it happens --
and every one of those was investigated by writing a bespoke probe and REPLAYING
FROM FRAME 0 to reach the fault again, minutes at a time, for a machine that was
already sitting right there when it broke.

So write it down. :func:`save_crash` dumps an ordinary snapshot plus a
``crash.json`` naming the fault, and an ordinary snapshot is RESUMABLE:

    load_snapshot_headless(crash_dir, game_root=...)   # you are at the fault

which is the whole point -- the fault becomes a starting position instead of a
destination. It costs one 1 MB write on a path that was about to fail anyway.

Loader-free by construction (see :mod:`dos_re.runtime_core`): a strict-VMless
runner is exactly the caller that needs this most, and it may not import
anything that reaches the EXE loader.
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from .snapshot import write_snapshot


def _regs(cpu) -> dict:
    s = cpu.s
    return {r: f"{getattr(s, r):04X}" for r in
            ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp",
             "cs", "ds", "es", "ss", "ip", "flags")}


def save_crash(rt, out_dir: str | Path, *, exc: BaseException | None = None,
               status: str = "crash", trace_tail=(), **context) -> Path:
    """Snapshot ``rt`` where it stands and record why. Returns the directory.

    ``context`` is whatever the caller knows that the machine does not -- the
    frame number, the park counts, which demo was replaying. Put it in: the
    machine state says WHERE it broke and the context says WHEN, and the second
    question is usually the harder one to answer afterwards.

    Never raises: this runs on a path that is already failing, and a crash
    handler that crashes costs the report it was trying to save. A failure to
    write is reported and swallowed.
    """
    out = Path(out_dir)
    try:
        cpu = rt.cpu
        write_snapshot(rt, out, status=status,
                       steps=int(getattr(cpu, "instruction_count", 0)),
                       trace_tail=trace_tail)
        info = {
            "status": status,
            "where": f"{cpu.s.cs:04X}:{cpu.s.ip:04X}",
            "registers": _regs(cpu),
            "steps": int(getattr(cpu, "instruction_count", 0)),
            "context": {k: v for k, v in context.items()},
        }
        if exc is not None:
            info["exception"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))[-4000:],
            }
        (out / "crash.json").write_text(json.dumps(info, indent=2) + "\n",
                                        encoding="utf-8")
        return out
    except Exception as write_failed:            # noqa: BLE001
        print(f"[crash] could not save the crash snapshot to {out}: "
              f"{type(write_failed).__name__}: {write_failed}")
        return out


def crash_dir(root: str | Path, name: str, stamp: str) -> Path:
    """``root/name_stamp`` -- a per-fault directory.

    ``stamp`` is the caller's: this module does not read the clock, so a run is
    reproducible and a test can pin the path.
    """
    return Path(root) / f"{name}_{stamp}"
