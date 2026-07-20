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
import re
import traceback
from pathlib import Path

# ``write_snapshot`` reaches the CPU, and the CPUless backend -- which has no
# interpreter at all and enforces that with a purity lint -- is a caller that
# needs this module MORE than the VMless one, not less. So the import is lazy
# and lives in save_crash; save_crash_headless below needs no CPU whatsoever.

#: A recovered function's module/qualname is ``func_CCCC_IIII`` -- its game
#: address is in the name, so a Python traceback through a recovered corpus IS
#: the game's own call stack. Extracting it is game-agnostic.
_RECOVERED_FRAME = re.compile(r"func_([0-9a-fA-F]{4})_([0-9a-fA-F]{4})$")
#: Fail-loud witnesses embed the address they refused at ("at 1010:2F57").
_WITNESS_ADDR = re.compile(r"\b([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\b")


def recovered_call_chain(exc: BaseException) -> list[str]:
    """The game's own call path to the failure, as ``CS:IP`` in call order.

    A recovered corpus puts one Python frame per recovered function on the
    stack, so the traceback carries the game's call chain -- but buried among
    emitter boilerplate and 300-column argument lines, which is why every
    investigation re-derived it by hand. Consecutive repeats collapse (a
    self-recursive helper would otherwise bury the shape).
    """
    chain: list[str] = []
    for frame, _lineno in traceback.walk_tb(exc.__traceback__):
        m = _RECOVERED_FRAME.search(frame.f_code.co_name)
        if m:
            addr = f"{m.group(1).upper()}:{m.group(2).upper()}"
            if not chain or chain[-1] != addr:
                chain.append(addr)
    return chain


def witness_address(exc: BaseException) -> str | None:
    """The ``CS:IP`` a fail-loud stub refused at, if the message carries one."""
    m = _WITNESS_ADDR.search(str(exc))
    return f"{m.group(1).upper()}:{m.group(2).upper()}" if m else None


def save_crash_headless(out_dir: str | Path, *, mem, dos,
                        exc: BaseException | None = None,
                        status: str = "crash", **context) -> Path:
    """:func:`save_crash` for a runtime with NO CPU (the CPUless backend).

    Writes the same shape -- ``memory_1mb.bin`` + ``state.json`` + ``crash.json``
    -- from the memory image and device model alone, so the dump reloads through
    ``snapshot_headless``. Registers are absent because no CPU exists; the
    recovered call chain replaces them as the "where", and it is strictly more
    useful (it names the game functions, not one instruction).

    Never raises, for the same reason :func:`save_crash` does not.
    """
    out = Path(out_dir)
    try:
        from .snapshot_headless import capture_dos_state    # CPU-free
        out.mkdir(parents=True, exist_ok=True)
        (out / "memory_1mb.bin").write_bytes(bytes(mem.data))
        (out / "state.json").write_text(
            json.dumps({"status": status, "dos": capture_dos_state(dos, mem)},
                       indent=1), encoding="utf-8")
        info = {
            "status": status,
            "where": witness_address(exc) if exc is not None else None,
            "recovered_call_chain": recovered_call_chain(exc) if exc else [],
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


def _regs(cpu) -> dict:
    s = cpu.s
    return {r: f"{getattr(s, r):04X}" for r in
            ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp",
             "cs", "ds", "es", "ss", "ip", "flags")}


def save_crash(rt, out_dir: str | Path, *, exc: BaseException | None = None,
               status: str = "crash", trace_tail=(), **context) -> Path:
    """Snapshot ``rt`` where it stands and record why. Returns the directory.

    ``context`` is whatever the caller knows that the machine does not -- the
    frame number, the park counts, which replay was replaying. Put it in: the
    machine state says WHERE it broke and the context says WHEN, and the second
    question is usually the harder one to answer afterwards.

    Never raises: this runs on a path that is already failing, and a crash
    handler that crashes costs the report it was trying to save. A failure to
    write is reported and swallowed.
    """
    out = Path(out_dir)
    try:
        from .snapshot import write_snapshot      # lazy: keeps this module CPU-free
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
            # The lifted/recovered call chain, when the fault came through one:
            # CS:IP alone names an instruction, this names the game's call path.
            chain = recovered_call_chain(exc)
            if chain:
                info["recovered_call_chain"] = chain
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
