from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .cpu import HaltExecution, UnsupportedInstruction
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
        "dos": {
            "video_mode": rt.dos.video_mode,
            "video_page": rt.dos.video_page,
            "text_mode_active": rt.dos.text_mode_active,
            "cursor_row": rt.dos.cursor_row,
            "cursor_col": rt.dos.cursor_col,
            "ticks": rt.dos.ticks,
            "vga_status_reads": rt.dos.vga_status_reads,
            "vga_palette": [list(rgb) for rgb in getattr(rt.dos, "vga_palette", [])],
            "dac_write_index": getattr(rt.dos, "_dac_write_index", 0),
            "dac_read_index": getattr(rt.dos, "_dac_read_index", 0),
            "dac_component": getattr(rt.dos, "_dac_component", 0),
            "dac_latch": list(getattr(rt.dos, "_dac_latch", [])),
            "pit_channel2_access": rt.dos._pit_channel2_access,
            "pit_channel2_latch": rt.dos._pit_channel2_latch,
            "pit_channel2_write_low": rt.dos._pit_channel2_write_low,
            "pit_channel2_reload": rt.dos.pit_channel2_reload,
            "pit_channel0_access": getattr(rt.dos, "_pit_channel0_access", 3),
            "pit_channel0_latch": getattr(rt.dos, "_pit_channel0_latch", 0),
            "pit_channel0_write_low": getattr(rt.dos, "_pit_channel0_write_low", True),
            "pit_channel0_reload": getattr(rt.dos, "pit_channel0_reload", 0),
            "speaker_control": rt.dos.speaker_control,
            "opl_selected_register": rt.dos.opl_selected_register,
            "opl_status": rt.dos.opl_status,
            "opl_registers": {f"{reg:02X}": value for reg, value in sorted(rt.dos.opl_registers.items())},
            "ega_planar": rt.program.memory.ega_planar,
            "ega_map_mask": rt.program.memory.ega_map_mask,
            "ega_read_plane": rt.program.memory.ega_read_plane,
            "ega_data_rotate": getattr(rt.program.memory, "ega_data_rotate", 0),
            "ega_logical_op": getattr(rt.program.memory, "ega_logical_op", 0),
            "ega_write_mode": getattr(rt.program.memory, "ega_write_mode", 0),
            "ega_set_reset": getattr(rt.program.memory, "ega_set_reset", 0),
            "ega_enable_set_reset": getattr(rt.program.memory, "ega_enable_set_reset", 0),
            "ega_bit_mask": getattr(rt.program.memory, "ega_bit_mask", 0xFF),
            "ega_latches": list(getattr(rt.program.memory, "ega_latches", [0, 0, 0, 0])),
            "ega_display_start": rt.program.memory.ega_display_start,
            "next_alloc_segment": rt.dos.next_alloc_segment,
            "allocation_limit_segment": rt.dos.allocation_limit_segment,
            "allocations": {f"{seg:04X}": size for seg, size in sorted(rt.dos.allocations.items())},
            "open_files": {
                str(handle): {"path": str(f.path), "pos": f.pos, "size": len(f.data)}
                for handle, f in rt.dos.files.items()
            },
            # Console input state IS machine state: a demo recorded across a
            # console-read boot (Lemmings' machine-type menu) replays from its
            # start snapshot and blocks forever if the queued keys vanish.
            "key_queue": list(getattr(rt.dos, "key_queue", ())),
            "pending_console_scancode": getattr(rt.dos, "pending_console_scancode", None),
            "console_input_fallback": getattr(rt.dos, "console_input_fallback", None),
            "stdout_tail": "".join(rt.dos.stdout)[-4096:],
            "port_log_tail": rt.dos.port_log[-128:],
        },
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
    return rt


# The EXE-free restore path lives in a separate module so the strict-VMless
# import graph never reaches this module's ``create_runtime`` loader edge
# (scripts/lint_vmless_independence.py).  Re-exported here for callers that
# already import restore helpers from dos_re.snapshot.
from .snapshot_headless import (  # noqa: E402
    load_snapshot_headless, _restore_dos_state, _restore_speaker_from_port_log_tail)
