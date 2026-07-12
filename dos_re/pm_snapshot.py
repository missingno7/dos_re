"""Save/load snapshots of a protected-mode (DOS/4GW) runtime.

The PM counterpart of :mod:`dos_re.snapshot`: capture everything a
:class:`~dos_re.runtime.PMRuntime` needs to resume deterministically — CPU
registers/segments/x87/selector table, the flat memory image, the DOS/4GW
host (DPMI allocator, KBC/key queues, VGA sequencer planes + DAC, open file
handles by path+position) — into a directory of ``state.json`` + compressed
binaries.  Loading rebuilds the runtime from the EXE and overlays the state.

Resume proof obligation: a snapshot taken mid-run and resumed must produce
the same execution as never having stopped (the determinism test in the
port's suite runs exactly that comparison).
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

STATE_FILE = "pm_state.json"
MEM_FILE = "pm_mem.bin.zlib"
PLANES_FILE = "pm_planes.bin.zlib"


def save_pm_snapshot(rt, directory: str | Path) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cpu, dos, mem = rt.cpu, rt.dos, rt.mem
    vga = dos.vga
    state = {
        "cpu": {
            "r": list(cpu.r), "eip": cpu.eip, "eflags": cpu.eflags,
            "seg": dict(cpu.seg), "sbase": dict(cpu.sbase),
            "selector_bases": {str(k): v for k, v in cpu.selector_bases.items()},
            "st": list(cpu.st), "fcw": cpu.fcw, "fsw": cpu.fsw,
            "cr": {str(k): v for k, v in cpu.cr.items()},
            "halted": cpu.halted, "instruction_count": cpu.instruction_count,
        },
        "dos": {
            "pm_vectors": {str(k): list(v) for k, v in dos.pm_vectors.items()},
            "dos_next": dos.dos_next, "dos_end": dos.dos_end,
            "dos_blocks": {str(k): list(v) for k, v in dos.dos_blocks.items()},
            "next_selector": dos._next_selector,
            "heap_next": dos._heap_next, "heap_end": dos._heap_end,
            "key_queue": list(dos.key_queue), "kbc_queue": list(dos.kbc_queue),
            "kbc_param_cmd": dos._kbc_param_cmd,
            "video_mode": getattr(dos, "video_mode", None),
            "dta": dos.dta, "exit_code": dos.exit_code,
            "vga_status_reads": dos.vga_status_reads,
            "dac": list(dos.dac),
            "dac_write_index": dos.dac_write_index, "dac_rgb_phase": dos.dac_rgb_phase,
            "mouse": [getattr(dos, "mouse_x", 320), getattr(dos, "mouse_y", 100),
                      getattr(dos, "mouse_buttons", 0)],
            "timer_period_instructions": dos.timer_period_instructions,
            "timer_next": dos._timer_next,
            "files": {str(h): [f.name, f.tell(), f.mode]
                      for h, f in dos.files.items()},
            "next_handle": dos._next_handle,
        },
        "vga": {
            "map_mask": vga.map_mask, "read_map": vga.read_map,
            "write_mode": vga.write_mode, "bit_mask": vga.bit_mask,
            "chain4": vga.chain4, "display_start": vga.display_start,
            "crtc": list(vga.crtc), "latches": list(vga.latches),
            "seq_index": vga.seq_index, "gc_index": vga.gc_index,
            "crtc_index": vga.crtc_index,
        },
        "mem_size": mem.size,
    }
    (d / STATE_FILE).write_text(json.dumps(state))
    (d / MEM_FILE).write_bytes(zlib.compress(bytes(mem.data), 6))
    (d / PLANES_FILE).write_bytes(zlib.compress(b"".join(vga.planes), 6))
    return d


def load_pm_snapshot(exe_path: str | Path, directory: str | Path, *,
                     game_root: str | Path | None = None):
    from .runtime import create_pm_runtime

    d = Path(directory)
    state = json.loads((d / STATE_FILE).read_text())
    rt = create_pm_runtime(exe_path, game_root=game_root,
                           ram_bytes=state["mem_size"])
    cpu, dos, mem = rt.cpu, rt.dos, rt.mem

    mem.data[:] = zlib.decompress((d / MEM_FILE).read_bytes())
    planes = zlib.decompress((d / PLANES_FILE).read_bytes())
    vga = dos.vga
    for i in range(4):
        vga.planes[i][:] = planes[i * 0x10000:(i + 1) * 0x10000]

    c = state["cpu"]
    cpu.r[:] = c["r"]
    cpu.eip = c["eip"]
    cpu.eflags = c["eflags"]
    cpu.seg.update(c["seg"])
    cpu.sbase.update(c["sbase"])
    cpu.selector_bases = {int(k): v for k, v in c["selector_bases"].items()}
    cpu.st = list(c["st"])
    cpu.fcw, cpu.fsw = c["fcw"], c["fsw"]
    cpu.cr = {int(k): v for k, v in c["cr"].items()}
    cpu.halted = c["halted"]
    cpu.instruction_count = c["instruction_count"]

    s = state["dos"]
    dos.pm_vectors.clear()
    dos.pm_vectors.update({int(k): tuple(v) for k, v in s["pm_vectors"].items()})
    dos.dos_next, dos.dos_end = s["dos_next"], s["dos_end"]
    dos.dos_blocks = {int(k): tuple(v) for k, v in s["dos_blocks"].items()}
    dos._next_selector = s["next_selector"]
    dos._heap_next, dos._heap_end = s["heap_next"], s["heap_end"]
    dos.key_queue = list(s["key_queue"])
    dos.kbc_queue = list(s["kbc_queue"])
    dos._kbc_param_cmd = s["kbc_param_cmd"]
    if s["video_mode"] is not None:
        dos.video_mode = s["video_mode"]
    dos.dta = s["dta"]
    dos.exit_code = s["exit_code"]
    dos.vga_status_reads = s["vga_status_reads"]
    dos.dac[:] = bytes(s["dac"])
    dos.dac_write_index = s["dac_write_index"]
    dos.dac_rgb_phase = s["dac_rgb_phase"]
    dos.mouse_x, dos.mouse_y, dos.mouse_buttons = s["mouse"]
    dos.timer_period_instructions = s["timer_period_instructions"]
    dos._timer_next = s["timer_next"]
    dos._next_handle = s["next_handle"]
    for h, (name, pos, mode) in s["files"].items():
        f = open(name, mode if "b" in mode else mode + "b")
        f.seek(pos)
        dos.files[int(h)] = f

    v = state["vga"]
    vga.map_mask = v["map_mask"]
    vga.read_map = v["read_map"]
    vga.write_mode = v["write_mode"]
    vga.bit_mask = v["bit_mask"]
    vga.chain4 = v["chain4"]
    vga.display_start = v["display_start"]
    vga.crtc[:] = bytes(v["crtc"])
    vga.latches = list(v["latches"])
    vga.seq_index, vga.gc_index, vga.crtc_index = (
        v["seq_index"], v["gc_index"], v["crtc_index"])
    mem.vga = None if vga.chain4 else vga
    return rt
