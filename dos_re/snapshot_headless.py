"""EXE-free snapshot restore — the load path that never touches an executable.

This module is deliberately split out of :mod:`dos_re.snapshot` so that the
strict-VMless runtime can import ``load_snapshot_headless`` (and the shared
``_restore_dos_state``) WITHOUT pulling in the EXE-based ``load_snapshot`` →
``create_runtime`` → ``load_mz_program`` loader.  The independence lint
(``scripts/lint_vmless_independence.py``) walks the import graph, and the whole
point is that this graph contains no loader edge (dos_re/docs/dos_re_2.0.md
§"The EXE-independence wall").

Nothing here parses an executable: the runtime shell is built from the
snapshot's own memory image + register state via
:func:`dos_re.runtime.create_runtime_from_image`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ANNOTATION ONLY.  Importing Runtime at module level would drag
    # runtime_core -> .cpu (CPU8086/CPUState) into every importer of this
    # module, which defeats the split this file exists for: the CPUless runner
    # imports _restore_dos_state and must not acquire a CPU carrier by doing
    # so.  `from __future__ import annotations` keeps the signatures readable
    # without the runtime edge.  load_snapshot_headless already imports the
    # carrier lazily, inside the function -- so only a caller that actually
    # builds a VM shell pays for it (measured: this edge was importing
    # dos_re.cpu on every play_cpuless boot, 2026-07-17).
    from .runtime_core import Runtime


def load_snapshot_headless(
    snapshot_dir: str | Path,
    *,
    game_root: str | Path,
    memory_name: str = "memory_1mb.bin",
) -> Runtime:
    """Restore a snapshot WITHOUT the original executable.

    The EXE-free analogue of :func:`dos_re.snapshot.load_snapshot`: it builds the
    runtime shell from the snapshot's own memory image and register state via
    :func:`dos_re.runtime.create_runtime_from_image` instead of re-parsing the
    binary.  This is what a data-only boot image is loaded through — the
    strict-VMless runtime never touches ``vgalemmi.exe``.  ``game_root`` is
    required (there is no ``exe_path`` to derive it from).
    """
    from .cpu import CPUState
    from .runtime_core import create_runtime_from_image

    snap = Path(snapshot_dir)
    meta = json.loads((snap / "state.json").read_text(encoding="utf-8"))
    memory_image = (snap / memory_name).read_bytes()
    state = CPUState(**meta["cpu"])
    rt = create_runtime_from_image(
        memory_image, state,
        game_root=game_root,
        psp_segment=meta.get("program", {}).get("psp_segment", 0),
        program_meta=meta.get("program"))
    _restore_dos_state(rt, meta.get("dos", {}))
    # VIRTUAL TIME IS MACHINE STATE.  The PIT down-counter is derived from
    # instruction_count (dos._pit_channel0_live_value), so the phase a program
    # can MEASURE (latch port 43h, read port 40h) is part of the machine, the
    # same way the DAC or the tick count is.  write_snapshot records it as
    # "steps"; dropping it here restarted every restored runtime at t=0 while
    # the interpreted oracle arrived with the loader's count -- invisible until
    # a program measured absolute phase.  VGA Lemmings' High Performance PC
    # timer calibration (1010:15AD/1602) does exactly that, and diverged by
    # precisely (steps * 3) mod 0x10000 PIT ticks (2026-07-17).
    rt.cpu.instruction_count = int(meta.get("steps") or 0)
    return rt


def capture_dos_state(dos, memory) -> dict:
    """Serialise the DOS/EGA/PIT/OPL/console machine state to a plain dict.

    The exact inverse of :func:`_restore_dos_state`, and like it CPU-FREE: it
    reads only the device model and the memory's EGA latch/mode state, never a
    CPU.  :func:`dos_re.snapshot.write_snapshot` builds its ``"dos"`` section
    from this, and a CPU-free backend (the CPUless runtime) can persist its own
    machine state for a post-mortem without an interpreter existing.

    Keep the key set in lockstep with ``_restore_dos_state``: a field captured
    here but not restored there is silently lost on reload.
    """
    return {
        "video_mode": dos.video_mode,
        "video_page": dos.video_page,
        "text_mode_active": dos.text_mode_active,
        "cursor_row": dos.cursor_row,
        "cursor_col": dos.cursor_col,
        "ticks": dos.ticks,
        "vga_status_reads": dos.vga_status_reads,
        "vga_palette": [list(rgb) for rgb in getattr(dos, "vga_palette", [])],
        "dac_write_index": getattr(dos, "_dac_write_index", 0),
        "dac_read_index": getattr(dos, "_dac_read_index", 0),
        "dac_component": getattr(dos, "_dac_component", 0),
        "dac_latch": list(getattr(dos, "_dac_latch", [])),
        "pit_channel2_access": dos._pit_channel2_access,
        "pit_channel2_latch": dos._pit_channel2_latch,
        "pit_channel2_write_low": dos._pit_channel2_write_low,
        "pit_channel2_reload": dos.pit_channel2_reload,
        "pit_channel0_access": getattr(dos, "_pit_channel0_access", 3),
        "pit_channel0_latch": getattr(dos, "_pit_channel0_latch", 0),
        "pit_channel0_write_low": getattr(dos, "_pit_channel0_write_low", True),
        "pit_channel0_reload": getattr(dos, "pit_channel0_reload", 0),
        "pit_channel0_anchor_ticks": getattr(dos, "pit_channel0_anchor_ticks", 0),
        "speaker_control": dos.speaker_control,
        "opl_selected_register": dos.opl_selected_register,
        "opl_status": dos.opl_status,
        "opl_registers": {f"{reg:02X}": value
                          for reg, value in sorted(dos.opl_registers.items())},
        "ega_planar": memory.ega_planar,
        "ega_map_mask": memory.ega_map_mask,
        "ega_read_plane": memory.ega_read_plane,
        "ega_data_rotate": getattr(memory, "ega_data_rotate", 0),
        "ega_logical_op": getattr(memory, "ega_logical_op", 0),
        "ega_write_mode": getattr(memory, "ega_write_mode", 0),
        "ega_set_reset": getattr(memory, "ega_set_reset", 0),
        "ega_enable_set_reset": getattr(memory, "ega_enable_set_reset", 0),
        "ega_bit_mask": getattr(memory, "ega_bit_mask", 0xFF),
        "ega_latches": list(getattr(memory, "ega_latches", [0, 0, 0, 0])),
        "ega_display_start": memory.ega_display_start,
        "next_alloc_segment": dos.next_alloc_segment,
        "allocation_limit_segment": dos.allocation_limit_segment,
        "allocations": {f"{seg:04X}": size
                        for seg, size in sorted(dos.allocations.items())},
        "open_files": {
            str(handle): {"path": str(f.path), "pos": f.pos, "size": len(f.data)}
            for handle, f in dos.files.items()
        },
        # Console input state IS machine state: a demo recorded across a
        # console-read boot (Lemmings' machine-type menu) replays from its
        # start snapshot and blocks forever if the queued keys vanish.
        "key_queue": list(getattr(dos, "key_queue", ())),
        "pending_console_scancode": getattr(dos, "pending_console_scancode", None),
        "console_input_fallback": getattr(dos, "console_input_fallback", None),
        "stdout_tail": "".join(dos.stdout)[-4096:],
        "port_log_tail": dos.port_log[-128:],
    }


def _restore_dos_state(rt: Runtime, dos_meta: dict) -> Runtime:
    """Apply the persisted DOS/EGA/PIT/OPL/console state onto a built runtime.

    Shared by :func:`dos_re.snapshot.load_snapshot` (EXE-based shell) and
    :func:`load_snapshot_headless` (EXE-free shell): both restore the same
    machine state; only the way the shell is constructed differs.
    """
    from .dos import FileHandle
    rt.dos.video_mode = dos_meta.get("video_mode", rt.dos.video_mode)
    rt.dos.video_page = dos_meta.get("video_page", rt.dos.video_page)
    if "text_mode_active" in dos_meta:
        rt.dos.text_mode_active = dos_meta["text_mode_active"]
    else:
        rt.dos.text_mode_active = False
    rt.dos.cursor_row = dos_meta.get("cursor_row", rt.dos.cursor_row)
    rt.dos.cursor_col = dos_meta.get("cursor_col", rt.dos.cursor_col)
    rt.dos.ticks = dos_meta.get("ticks", rt.dos.ticks)
    rt.dos.vga_status_reads = dos_meta.get("vga_status_reads", rt.dos.vga_status_reads)
    if "vga_palette" in dos_meta:
        rt.dos.vga_palette = [tuple(map(int, rgb)) for rgb in dos_meta["vga_palette"]]
    rt.dos._dac_write_index = dos_meta.get("dac_write_index", rt.dos._dac_write_index)
    rt.dos._dac_read_index = dos_meta.get("dac_read_index", rt.dos._dac_read_index)
    rt.dos._dac_component = dos_meta.get("dac_component", rt.dos._dac_component)
    rt.dos._dac_latch = list(dos_meta.get("dac_latch", rt.dos._dac_latch))
    rt.dos._pit_channel2_access = dos_meta.get("pit_channel2_access", rt.dos._pit_channel2_access)
    rt.dos._pit_channel2_latch = dos_meta.get("pit_channel2_latch", rt.dos._pit_channel2_latch)
    rt.dos._pit_channel2_write_low = dos_meta.get("pit_channel2_write_low", rt.dos._pit_channel2_write_low)
    rt.dos.pit_channel2_reload = dos_meta.get("pit_channel2_reload", rt.dos.pit_channel2_reload)
    rt.dos._pit_channel0_access = dos_meta.get("pit_channel0_access", rt.dos._pit_channel0_access)
    rt.dos._pit_channel0_latch = dos_meta.get("pit_channel0_latch", rt.dos._pit_channel0_latch)
    rt.dos._pit_channel0_write_low = dos_meta.get("pit_channel0_write_low", rt.dos._pit_channel0_write_low)
    rt.dos.pit_channel0_reload = dos_meta.get("pit_channel0_reload", rt.dos.pit_channel0_reload)
    rt.dos.pit_channel0_anchor_ticks = dos_meta.get("pit_channel0_anchor_ticks",
                                                    rt.dos.pit_channel0_anchor_ticks)
    rt.dos.speaker_control = dos_meta.get("speaker_control", rt.dos.speaker_control)
    rt.dos.opl_selected_register = dos_meta.get("opl_selected_register", rt.dos.opl_selected_register)
    rt.dos.opl_status = dos_meta.get("opl_status", rt.dos.opl_status)
    rt.dos.opl_registers = {int(reg, 16): int(value) for reg, value in dos_meta.get("opl_registers", {}).items()}
    if "pit_channel2_reload" not in dos_meta and "port_log_tail" in dos_meta:
        _restore_speaker_from_port_log_tail(rt, dos_meta.get("port_log_tail", ()))
    rt.program.memory.ega_planar = dos_meta.get("ega_planar", rt.program.memory.ega_planar)
    rt.program.memory.ega_map_mask = dos_meta.get("ega_map_mask", rt.program.memory.ega_map_mask)
    rt.program.memory.ega_read_plane = dos_meta.get("ega_read_plane", rt.program.memory.ega_read_plane)
    rt.program.memory.ega_data_rotate = dos_meta.get("ega_data_rotate", rt.program.memory.ega_data_rotate)
    rt.program.memory.ega_logical_op = dos_meta.get("ega_logical_op", rt.program.memory.ega_logical_op)
    rt.program.memory.ega_write_mode = dos_meta.get("ega_write_mode", rt.program.memory.ega_write_mode)
    rt.program.memory.ega_set_reset = dos_meta.get("ega_set_reset", rt.program.memory.ega_set_reset)
    rt.program.memory.ega_enable_set_reset = dos_meta.get("ega_enable_set_reset", rt.program.memory.ega_enable_set_reset)
    rt.program.memory.ega_bit_mask = dos_meta.get("ega_bit_mask", rt.program.memory.ega_bit_mask)
    rt.program.memory.ega_latches = list(dos_meta.get("ega_latches", rt.program.memory.ega_latches))
    rt.program.memory.ega_display_start = dos_meta.get("ega_display_start", rt.program.memory.ega_display_start)
    if "key_queue" in dos_meta:
        rt.dos.key_queue = [int(k) for k in dos_meta["key_queue"]]
        rt.dos.pending_console_scancode = dos_meta.get("pending_console_scancode")
        rt.dos.console_input_fallback = dos_meta.get("console_input_fallback")
    rt.dos.next_alloc_segment = dos_meta.get("next_alloc_segment", rt.dos.next_alloc_segment)
    rt.dos.allocation_limit_segment = dos_meta.get("allocation_limit_segment", rt.dos.allocation_limit_segment)
    rt.dos.allocations = {int(seg, 16): int(size) for seg, size in dos_meta.get("allocations", {}).items()}
    rt.dos.files.clear()
    for handle_text, file_meta in dos_meta.get("open_files", {}).items():
        path = Path(file_meta["path"])
        if not path.is_absolute():
            path = Path(path)
        if not path.exists():
            path = rt.dos.resolve_game_path(Path(file_meta["path"]).name)
        fh = FileHandle(path, bytearray(path.read_bytes()), pos=int(file_meta.get("pos", 0)))
        rt.dos.files[int(handle_text)] = fh
    if rt.dos.files:
        rt.dos.next_handle = max(rt.dos.files) + 1
    # Stash any persisted Sound Blaster state for the front-end to apply when it
    # attaches the SB (enable_sound_blaster); restore itself stays frontend-
    # agnostic and does not create audio hardware.
    rt.dos.sound_blaster_snapshot = dos_meta.get("sound_blaster")
    return rt


def _restore_speaker_from_port_log_tail(rt: Runtime, port_log_tail) -> None:
    """Best-effort PC-speaker state recovery for older snapshots.

    Pre-sound-state snapshots only stored the last few OUT instructions.  Replaying
    the speaker-related writes reconstructs the PIT channel-2 reload and port 61h
    gate when the tail contains the most recent tone setup, which is exactly the
    common F12-in-the-menu case.  The replay updates DOS hardware state only; it
    deliberately does not call a frontend speaker callback or append duplicate log
    entries.
    """
    saved_callback = rt.dos.speaker_callback
    rt.dos.speaker_callback = None
    try:
        for entry in port_log_tail or ():
            if not isinstance(entry, (list, tuple)) or len(entry) != 4:
                continue
            direction, port, value, bits = entry
            if direction != "out":
                continue
            port = int(port) & 0xFFFF
            if port not in (0x42, 0x43, 0x61):
                continue
            rt.dos._track_pc_speaker(rt.cpu, port, int(value), int(bits))
    finally:
        rt.dos.speaker_callback = saved_callback
