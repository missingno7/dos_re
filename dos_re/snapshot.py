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
            # from a detached generated-graph session is itself data-only.
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


# The EXE-free restore path lives in a separate module so release dependency
# closure never reaches this module's ``create_runtime`` loader edge.
from .snapshot_headless import (  # noqa: E402
    capture_dos_state, _restore_dos_state, _restore_speaker_from_port_log_tail)


def clone_runtime_state(src: Runtime) -> Runtime:
    """Return a detached in-memory clone of a real-mode runtime.

    Cloning is an in-memory snapshot operation. The
    dos_re 3.0 replay drivers use it for independent oracle/candidate runs;
    persistent divergence reproduction lives in :mod:`dos_re.replay` as a
    cached stable point plus annotation.
    """
    import copy
    from dataclasses import replace as dc_replace
    from .cpu import CPU8086
    from .dos import DOSMachine, FileHandle
    from .memory import Memory
    from .runtime import Runtime

    mem = Memory(0)
    mem.data = src.program.memory.data.copy()
    mem.size = src.program.memory.size
    for name in (
        "ega_planar", "ega_map_mask", "ega_read_plane", "ega_data_rotate",
        "ega_logical_op", "ega_write_mode", "ega_set_reset",
        "ega_enable_set_reset", "ega_bit_mask", "ega_display_start",
    ):
        setattr(mem, name, getattr(src.program.memory, name))
    mem.ega_latches = list(src.program.memory.ega_latches)

    dos = DOSMachine(src.dos.root)
    for name in (
        "next_handle", "next_alloc_segment", "allocation_limit_segment",
        "video_mode", "video_page", "text_mode_active", "cursor_row",
        "cursor_col", "ticks", "vga_status_reads", "_dac_write_index",
        "_dac_read_index", "_dac_component", "_pit_channel2_access",
        "_pit_channel2_latch", "_pit_channel2_write_low",
        "pit_channel2_reload", "speaker_control", "opl_selected_register",
        "opl_status", "_seq_index", "_crtc_index", "current_scancode",
        "console_input_fallback", "pending_console_scancode",
    ):
        setattr(dos, name, copy.deepcopy(getattr(src.dos, name)))
    dos.stdout = list(src.dos.stdout)
    dos.files = {
        handle: FileHandle(f.path, bytearray(f.data), f.pos, f.writable)
        for handle, f in src.dos.files.items()
    }
    dos.allocations = dict(src.dos.allocations)
    dos.vga_palette = [tuple(rgb) for rgb in src.dos.vga_palette]
    dos._dac_latch = list(src.dos._dac_latch)
    dos.opl_registers = dict(src.dos.opl_registers)
    dos.key_queue = list(src.dos.key_queue)
    dos.port_log = list(src.dos.port_log)

    cpu = CPU8086(mem, dc_replace(src.cpu.s))
    for name in (
        "halted", "call_depth", "instruction_count", "max_rep_count",
        "hook_verifier_verify_nested_calls",
    ):
        setattr(cpu, name, getattr(src.cpu, name))
    cpu.trace_enabled = False
    cpu.replacement_hooks = dict(src.cpu.replacement_hooks)
    cpu.hook_names = dict(src.cpu.hook_names)
    cpu.hook_verifier_passthrough = set(src.cpu.hook_verifier_passthrough)
    cpu.hook_verifier_live_passthrough_overrides = dict(
        src.cpu.hook_verifier_live_passthrough_overrides)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    program = copy.copy(src.program)
    program.memory = mem
    return Runtime(program, cpu, dos)


def capture_runtime_continuation(rt: Runtime, *, event_cursor: int):
    """Capture real-mode runtime state for a 3.0 replay profile.

    Unlike :func:`write_snapshot`, this returns the common in-memory
    ``ContinuationState`` consumed by replay caches. Static executable and
    selected implementation identities belong to ``ReplayExecutionIdentity``
    rather than the mutable continuation payload.
    """
    from .replay import ContinuationState

    if rt.dos.time_source is not None:
        raise ValueError(
            "cannot capture deterministic replay continuation with a wall-clock time source")

    dos_state = capture_dos_state(rt.dos, rt.program.memory)
    # A replay continuation is a closed deterministic machine state.  The
    # interactive player's save directory is an external host persistence
    # policy, not guest state: restoring it would let later replayed DOS opens
    # observe files changed after recording.  Preserve the in-memory file
    # overlay and open-file regions, but detach the host sink.
    dos_state["save_dir"] = None
    sb = getattr(rt.dos, "sound_blaster", None)
    if sb is not None:
        dos_state["sound_blaster"] = sb.snapshot_state()
    pic = getattr(rt.dos, "pic", None)
    if pic is not None:
        dos_state["pic"] = {"imr": pic.imr, "irr": pic.irr, "isr": pic.isr}
    regions = {"memory": bytes(rt.program.memory.data)}
    file_regions: dict[str, str] = {}
    for handle, file_handle in sorted(rt.dos.files.items()):
        name = f"dos-file-{handle}"
        regions[name] = bytes(file_handle.data)
        file_regions[str(handle)] = name
    dos_state["file_regions"] = file_regions
    return ContinuationState(
        schema_id="dos-re-real-mode-continuation-v1",
        metadata={
            "cpu": asdict(rt.cpu.s),
            "instruction_count": rt.cpu.instruction_count,
            "halted": rt.cpu.halted,
            "call_depth": rt.cpu.call_depth,
            "dos": dos_state,
        },
        regions=regions,
        event_cursor=event_cursor,
    ).normalized()


def apply_runtime_continuation(rt: Runtime, state) -> None:
    """Apply :func:`capture_runtime_continuation` to an existing runtime shell."""
    from .cpu import CPUState

    state = state.normalized()
    if state.schema_id != "dos-re-real-mode-continuation-v1":
        raise ValueError(f"not a real-mode continuation state: {state.schema_id!r}")
    if "memory" not in state.regions:
        raise ValueError("real-mode continuation requires the memory region")
    if len(state.regions["memory"]) != len(rt.program.memory.data):
        raise ValueError("real-mode continuation memory size mismatch")
    dos_state = state.metadata["dos"]
    for state_key, runtime_attribute in (
        ("pic", "pic"),
        ("sound_blaster", "sound_blaster"),
    ):
        state_has_device = dos_state.get(state_key) is not None
        runtime_has_device = getattr(rt.dos, runtime_attribute, None) is not None
        if state_has_device != runtime_has_device:
            raise ValueError(
                "real-mode continuation device topology mismatch for "
                f"{state_key}: state={'present' if state_has_device else 'absent'}, "
                f"runtime={'present' if runtime_has_device else 'absent'}"
            )
    rt.program.memory.data[:] = state.regions["memory"]
    rt.cpu.mem = rt.program.memory
    rt.cpu.s = CPUState(**state.metadata["cpu"])
    rt.cpu.instruction_count = int(state.metadata["instruction_count"])
    rt.cpu.halted = bool(state.metadata["halted"])
    rt.cpu.call_depth = int(state.metadata["call_depth"])
    _restore_dos_state(rt, dos_state)
    # Older ReplayArtifacts may have captured the viewer's save path.  Never
    # reactivate it during deterministic continuation restore.  This is
    # intentionally local to replay continuations; ordinary developer
    # snapshots retain their existing persistence semantics.
    rt.dos.save_dir = None
    for handle_text, region_name in dos_state.get("file_regions", {}).items():
        handle = int(handle_text)
        if handle not in rt.dos.files or region_name not in state.regions:
            raise ValueError(f"missing continuation state for DOS file handle {handle}")
        rt.dos.files[handle].data[:] = state.regions[region_name]
    pic_state = dos_state.get("pic")
    if pic_state is not None and getattr(rt.dos, "pic", None) is not None:
        rt.dos.pic.imr = int(pic_state["imr"])
        rt.dos.pic.irr = int(pic_state["irr"])
        rt.dos.pic.isr = int(pic_state["isr"])
    sb_state = dos_state.get("sound_blaster")
    sb = getattr(rt.dos, "sound_blaster", None)
    if sb_state is not None and sb is not None:
        sb.restore_state(sb_state)
        sb.rearm_after_restore()
