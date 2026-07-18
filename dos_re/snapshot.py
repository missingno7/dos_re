from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

# These are DEFINED in .x86 and merely re-exported by .cpu; importing them
# through the carrier dragged CPU8086 into every importer of this module --
# including the CPUless runner, whose whole contract is that it never acquires
# a CPU (measured 2026-07-17).  Import them from where they live.
from .x86 import HaltExecution, UnsupportedInstruction
if TYPE_CHECKING:
    # ANNOTATION ONLY -- see the note in snapshot_headless.  Runtime pulls
    # runtime_core -> .cpu, and write_snapshot is EXE-free and CPU-free by
    # design: the CPUless runner serializes its boundary park through it and
    # must not acquire an interpreter to do so.  load_snapshot (which really
    # does build a VM shell) imports the carrier lazily, inside the function.
    from .runtime_core import Runtime


def parse_addr(text: str) -> tuple[int, int]:
    cs, ip = text.split(":", 1)
    return int(cs, 16) & 0xFFFF, int(ip, 16) & 0xFFFF


def run_until(
    rt: Runtime,
    *,
    max_steps: int,
    stop_at: tuple[int, int] | None = None,
    trace_tail: int = 0,
) -> tuple[str, int, list[str]]:
    """Run the interpreter and optionally keep only the last N trace lines."""
    tail: deque[str] = deque(maxlen=trace_tail)
    rt.cpu.trace_enabled = trace_tail > 0
    steps = 0
    try:
        for steps in range(1, max_steps + 1):
            if stop_at is not None and rt.cpu.addr() == stop_at:
                return f"reached {stop_at[0]:04X}:{stop_at[1]:04X}", steps - 1, list(tail)
            rt.cpu.step()
            if rt.cpu.trace:
                tail.extend(rt.cpu.trace)
                rt.cpu.trace.clear()
        return "stopped after max steps", steps, list(tail)
    except HaltExecution:
        return "program halted", steps, list(tail)
    except UnsupportedInstruction as e:
        return f"unsupported instruction: {e}", steps, list(tail)
    except Exception as e:  # keep snapshots useful even during emulator bring-up
        return f"exception: {type(e).__name__}: {e}", steps, list(tail)


def write_snapshot(rt: Runtime, out_dir: str | Path, *, status: str, steps: int, trace_tail: Iterable[str] = ()) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "memory_1mb.bin").write_bytes(bytes(rt.program.memory.data))
    (out / "trace_tail.txt").write_text("\n".join(trace_tail) + ("\n" if trace_tail else ""), encoding="utf-8")
    meta = {
        "status": status,
        "steps": steps,
        "cpu": asdict(rt.cpu.s),
        "cpu_snapshot": rt.cpu.s.snapshot(),
        "program": {
            # A headless (EXE-free) runtime has no ``exe`` — a snapshot saved
            # from a strict-VMless session is itself data-only.
            "path": str(rt.program.exe.path) if rt.program.exe is not None
            else "<generated data-only boot image>",
            "psp_segment": rt.program.psp_segment,
            "load_segment": rt.program.load_segment,
            "entry_cs": rt.program.entry_cs,
            "entry_ip": rt.program.entry_ip,
            "initial_ss": rt.program.initial_ss,
            "initial_sp": rt.program.initial_sp,
            "load_module_size": len(rt.program.exe.load_module)
            if rt.program.exe is not None else 0,
            "overlay_size": len(rt.program.overlay),
        },
        # Built by the CPU-FREE capture_dos_state (inverse of
        # _restore_dos_state) so a CPU-free backend can persist the same
        # machine state without an interpreter existing.
        "dos": capture_dos_state(rt.dos, rt.program.memory),
        "hooks": {
            f"{cs:04X}:{ip:04X}": name for (cs, ip), name in sorted(rt.cpu.hook_names.items())
        },
    }
    # The emulated Sound Blaster / DMA programming is part of the machine state:
    # persist it so a save taken mid-playback resumes streaming (the front-end
    # re-attaches the SB and applies this via enable_sound_blaster).
    sound_blaster = getattr(rt.dos, "sound_blaster", None)
    if sound_blaster is not None:
        meta["dos"]["sound_blaster"] = sound_blaster.snapshot_state()
    (out / "state.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_snapshot(exe_path: str | Path, snapshot_dir: str | Path, *, game_root: str | Path | None = None) -> Runtime:
    """Create a Runtime from an existing snapshot directory.

    This is intentionally a developer/reverse-engineering helper: it restores
    CPU state, full 1MB memory, and simple DOS bookkeeping so investigation can
    continue from a known checkpoint instead of replaying the whole bootstrap.
    """
    from .cpu import CPUState
    from .runtime import create_runtime

    snap = Path(snapshot_dir)
    meta = json.loads((snap / "state.json").read_text(encoding="utf-8"))
    rt = create_runtime(exe_path, game_root=game_root)
    rt.program.memory.data[:] = (snap / "memory_1mb.bin").read_bytes()
    rt.cpu.mem = rt.program.memory
    rt.cpu.s = CPUState(**meta["cpu"])
    _restore_dos_state(rt, meta.get("dos", {}))
    # Virtual time is machine state (the PIT phase is derived from it) --
    # restore it exactly as the headless loader does; see the note there.
    rt.cpu.instruction_count = int(meta.get("steps") or 0)
    return rt


# The EXE-free restore path lives in a separate module so the strict-VMless
# import graph never reaches this module's ``create_runtime`` loader edge
# (scripts/lint_vmless_independence.py).  Re-exported here for callers that
# already import restore helpers from dos_re.snapshot.
from .snapshot_headless import (  # noqa: E402
    load_snapshot_headless, capture_dos_state, _restore_dos_state,
    _restore_speaker_from_port_log_tail)
