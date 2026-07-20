"""CPU-free capture and restore for DOS, device, and memory-controller state."""
from __future__ import annotations

from pathlib import Path


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
        "seq_index": getattr(dos, "_seq_index", 0),
        "seq_regs": {str(k): v for k, v in getattr(dos, "_seq_regs", {}).items()},
        "gc_index": getattr(dos, "_gc_index", 0),
        "gc_regs": {str(k): v for k, v in getattr(dos, "_gc_regs", {}).items()},
        "crtc_index": getattr(dos, "_crtc_index", 0),
        "crtc_regs": {str(k): v for k, v in getattr(dos, "_crtc_regs", {}).items()},
        "attr_index": getattr(dos, "_attr_index", 0),
        "attr_flipflop": getattr(dos, "_attr_flipflop", False),
        "attr_regs": {str(k): v for k, v in getattr(dos, "_attr_regs", {}).items()},
        "misc_output": getattr(dos, "_misc_output", 0xA3),
        "pit_channel2_access": dos._pit_channel2_access,
        "pit_channel2_latch": dos._pit_channel2_latch,
        "pit_channel2_write_low": dos._pit_channel2_write_low,
        "pit_channel2_reload": dos.pit_channel2_reload,
        "pit_channel0_access": getattr(dos, "_pit_channel0_access", 3),
        "pit_channel0_latch": getattr(dos, "_pit_channel0_latch", 0),
        "pit_channel0_write_low": getattr(dos, "_pit_channel0_write_low", True),
        "pit_channel0_reload": getattr(dos, "pit_channel0_reload", 0),
        "pit_channel0_anchor_ticks": getattr(dos, "pit_channel0_anchor_ticks", 0),
        "pit_channel0_read_latch": list(getattr(dos, "_pit_channel0_read_latch", [])),
        "vga_retrace_active_fraction": getattr(dos, "vga_retrace_active_fraction", 0.28),
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
        "ega_read_mode": getattr(memory, "ega_read_mode", 0),
        "ega_color_compare": getattr(memory, "ega_color_compare", 0),
        "ega_color_dont_care": getattr(memory, "ega_color_dont_care", 0x0F),
        "ega_latches": list(getattr(memory, "ega_latches", [0, 0, 0, 0])),
        "ega_display_start": memory.ega_display_start,
        "ega_pel_pan": getattr(memory, "ega_pel_pan", 0),
        "ega_display_enabled": getattr(memory, "ega_display_enabled", True),
        "ega_h_display_end": getattr(memory, "ega_h_display_end", 39),
        "ega_pan_active": getattr(memory, "ega_pan_active", False),
        "ega_pan_display_start": getattr(memory, "ega_pan_display_start", 0),
        "ega_pan_pel": getattr(memory, "ega_pan_pel", 0),
        "next_alloc_segment": dos.next_alloc_segment,
        "allocation_limit_segment": dos.allocation_limit_segment,
        "next_handle": dos.next_handle,
        "allocations": {f"{seg:04X}": size
                        for seg, size in sorted(dos.allocations.items())},
        "open_files": {
            str(handle): {"path": str(f.path), "pos": f.pos, "size": len(f.data),
                          "writable": bool(f.writable)}
            for handle, f in dos.files.items()
        },
        "file_overlay": {name: bytes(data).hex()
                         for name, data in sorted(getattr(dos, "file_overlay", {}).items())},
        "save_dir": None if getattr(dos, "save_dir", None) is None else str(dos.save_dir),
        # Console input state IS machine state: a replay recorded across a
        # console-read boot (Lemmings' machine-type menu) replays from its
        # start snapshot and blocks forever if the queued keys vanish.
        "key_queue": list(getattr(dos, "key_queue", ())),
        "pending_console_scancode": getattr(dos, "pending_console_scancode", None),
        "console_input_fallback": getattr(dos, "console_input_fallback", None),
        "current_scancode": getattr(dos, "current_scancode", 0),
        "kbd_output_buffer_full": getattr(dos, "kbd_output_buffer_full", False),
        "kbd_shift": getattr(dos, "kbd_shift", False),
        "kbd_ctrl": getattr(dos, "kbd_ctrl", False),
        "kbd_alt": getattr(dos, "kbd_alt", False),
        "kbd_caps": getattr(dos, "kbd_caps", False),
        "mouse_present": getattr(dos, "mouse_present", False),
        "mouse_x": getattr(dos, "mouse_x", 320),
        "mouse_y": getattr(dos, "mouse_y", 100),
        "mouse_buttons": getattr(dos, "mouse_buttons", 0),
        "mouse_range": list(getattr(dos, "mouse_range", [0, 639, 0, 199])),
        "strict_ports": getattr(dos, "strict_ports", False),
        "unmodeled_port_reads": [list(item) for item in getattr(dos, "unmodeled_port_reads", [])],
        "stdout_tail": "".join(dos.stdout)[-4096:],
        "port_log_tail": dos.port_log[-128:],
    }


def _restore_dos_state(rt, dos_meta: dict):
    """Apply the persisted DOS/EGA/PIT/OPL/console state onto a built runtime.

    Shared by machine-backed and CPU-free runtimes. Both restore the same
    continuation state; only the runtime shell differs.
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
    rt.dos._seq_index = dos_meta.get("seq_index", rt.dos._seq_index)
    rt.dos._seq_regs = {int(k): int(v) for k, v in dos_meta.get("seq_regs", {}).items()}
    rt.dos._gc_index = dos_meta.get("gc_index", rt.dos._gc_index)
    rt.dos._gc_regs = {int(k): int(v) for k, v in dos_meta.get("gc_regs", {}).items()}
    rt.dos._crtc_index = dos_meta.get("crtc_index", rt.dos._crtc_index)
    rt.dos._crtc_regs = {int(k): int(v) for k, v in dos_meta.get("crtc_regs", {}).items()}
    rt.dos._attr_index = dos_meta.get("attr_index", rt.dos._attr_index)
    rt.dos._attr_flipflop = dos_meta.get("attr_flipflop", rt.dos._attr_flipflop)
    rt.dos._attr_regs = {int(k): int(v) for k, v in dos_meta.get("attr_regs", {}).items()}
    rt.dos._misc_output = dos_meta.get("misc_output", rt.dos._misc_output)
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
    rt.dos._pit_channel0_read_latch = list(map(
        int, dos_meta.get("pit_channel0_read_latch", rt.dos._pit_channel0_read_latch)))
    rt.dos.vga_retrace_active_fraction = float(dos_meta.get(
        "vga_retrace_active_fraction", rt.dos.vga_retrace_active_fraction))
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
    rt.program.memory.ega_read_mode = dos_meta.get("ega_read_mode", rt.program.memory.ega_read_mode)
    rt.program.memory.ega_color_compare = dos_meta.get("ega_color_compare", rt.program.memory.ega_color_compare)
    rt.program.memory.ega_color_dont_care = dos_meta.get("ega_color_dont_care", rt.program.memory.ega_color_dont_care)
    rt.program.memory.ega_latches = list(dos_meta.get("ega_latches", rt.program.memory.ega_latches))
    rt.program.memory.ega_display_start = dos_meta.get("ega_display_start", rt.program.memory.ega_display_start)
    rt.program.memory.ega_pel_pan = dos_meta.get("ega_pel_pan", rt.program.memory.ega_pel_pan)
    rt.program.memory.ega_display_enabled = dos_meta.get("ega_display_enabled", rt.program.memory.ega_display_enabled)
    rt.program.memory.ega_h_display_end = dos_meta.get("ega_h_display_end", rt.program.memory.ega_h_display_end)
    rt.program.memory.ega_pan_active = dos_meta.get("ega_pan_active", rt.program.memory.ega_pan_active)
    rt.program.memory.ega_pan_display_start = dos_meta.get("ega_pan_display_start", rt.program.memory.ega_pan_display_start)
    rt.program.memory.ega_pan_pel = dos_meta.get("ega_pan_pel", rt.program.memory.ega_pan_pel)
    if "key_queue" in dos_meta:
        rt.dos.key_queue = [int(k) for k in dos_meta["key_queue"]]
        rt.dos.pending_console_scancode = dos_meta.get("pending_console_scancode")
        rt.dos.console_input_fallback = dos_meta.get("console_input_fallback")
    for name in ("current_scancode", "kbd_output_buffer_full", "kbd_shift",
                 "kbd_ctrl", "kbd_alt", "kbd_caps", "mouse_present", "mouse_x",
                 "mouse_y", "mouse_buttons", "strict_ports"):
        if name in dos_meta:
            setattr(rt.dos, name, dos_meta[name])
    if "mouse_range" in dos_meta:
        rt.dos.mouse_range = list(map(int, dos_meta["mouse_range"]))
    if "unmodeled_port_reads" in dos_meta:
        rt.dos.unmodeled_port_reads = [tuple(map(int, item))
                                       for item in dos_meta["unmodeled_port_reads"]]
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
        fh = FileHandle(path, bytearray(path.read_bytes()), pos=int(file_meta.get("pos", 0)),
                        writable=bool(file_meta.get("writable", False)))
        rt.dos.files[int(handle_text)] = fh
    rt.dos.next_handle = int(dos_meta.get(
        "next_handle", max(rt.dos.files, default=4) + 1))
    rt.dos.file_overlay = {
        str(name): bytearray.fromhex(encoded)
        for name, encoded in dos_meta.get("file_overlay", {}).items()
    }
    save_dir = dos_meta.get("save_dir")
    rt.dos.save_dir = None if save_dir is None else Path(save_dir)
    # Stash any persisted Sound Blaster state for the front-end to apply when it
    # attaches the SB (enable_sound_blaster); restore itself stays frontend-
    # agnostic and does not create audio hardware.
    rt.dos.sound_blaster_snapshot = dos_meta.get("sound_blaster")
    return rt


def _restore_speaker_from_port_log_tail(rt, port_log_tail) -> None:
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
