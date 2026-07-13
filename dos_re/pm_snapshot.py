"""Save/load/clone the state of a protected-mode (DOS/4GW) runtime.

The PM counterpart of :mod:`dos_re.snapshot`: capture everything a
:class:`~dos_re.runtime.PMRuntime` needs to resume deterministically — CPU
registers/segments/x87/selector table, the flat memory image, the DOS/4GW
host (DPMI allocator, KBC/key queues, VGA sequencer planes + DAC, open file
handles by path+position).

One state list, three consumers: :func:`save_pm_snapshot` /
:func:`load_pm_snapshot` serialize to a directory (``state.json`` +
compressed binaries); :func:`clone_pm_runtime` transfers the same state to a
fresh in-memory runtime (the differential verifier's pre-state / oracle
clones).  Keeping capture/apply shared means a field added to one cannot be
silently missed by the others.

Resume proof obligation: a snapshot taken mid-run and resumed must produce
the same execution as never having stopped (the port suite runs exactly that
comparison; the clone path is proven by the PM hook verifier's tests).
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

STATE_FILE = "pm_state.json"
MEM_FILE = "pm_mem.bin.zlib"
PLANES_FILE = "pm_planes.bin.zlib"


def capture_pm_state(rt) -> dict:
    """Everything except the two big binaries (memory image, VGA planes)."""
    cpu, dos = rt.cpu, rt.dos
    vga = dos.vga
    return {
        "cpu": {
            "r": list(cpu.r), "eip": cpu.eip, "eflags": cpu.eflags,
            "seg": dict(cpu.seg), "sbase": dict(cpu.sbase),
            "selector_bases": {str(k): v for k, v in cpu.selector_bases.items()},
            "st": list(cpu.st), "fcw": cpu.fcw, "fsw": cpu.fsw,
            "cr": {str(k): v for k, v in cpu.cr.items()},
            "halted": cpu.halted, "instruction_count": cpu.instruction_count,
            # The every-16-instructions IRQ-poll phase: a resumed run must keep
            # the same phase, or a hardware IRQ (SB block, timer) lands at a
            # different instruction than in the run that saved — enough to make
            # a demo replay diverge one frame in.
            "irq_decim": cpu._irq_decim,
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
            # The INT 33h virtual range (AX=7/8) the game programmed — without
            # it a resume reverts to the unclamped default and the paddle flies.
            "mouse_range": list(getattr(dos, "mouse_range", [0, 639, 0, 199])),
            "timer_period_instructions": dos.timer_period_instructions,
            "timer_next": dos._timer_next,
            "files": {str(h): [f.name, f.tell(), f.mode]
                      for h, f in dos.files.items()},
            "next_handle": dos._next_handle,
            "pic": [dos.pic.imr, dos.pic.irr, dos.pic.isr],
        },
        "vga": {
            "map_mask": vga.map_mask, "read_map": vga.read_map,
            "write_mode": vga.write_mode, "bit_mask": vga.bit_mask,
            "chain4": vga.chain4, "display_start": vga.display_start,
            "crtc": list(vga.crtc), "latches": list(vga.latches),
            "seq_index": vga.seq_index, "gc_index": vga.gc_index,
            "crtc_index": vga.crtc_index,
        },
        "mem_size": rt.mem.size,
        # Attached Sound Blaster (optional device): config + DSP/DMA state so
        # a resumed run keeps streaming (sblaster.snapshot_state contract).
        "sb": None if dos.sound_blaster is None else {
            "base": dos.sound_blaster.base, "irq": dos.sound_blaster.irq,
            "dma": dos.sound_blaster.dma,
            "state": dos.sound_blaster.snapshot_state(),
        },
    }


def apply_pm_state(rt, state: dict, mem_bytes, planes_bytes) -> None:
    cpu, dos, mem = rt.cpu, rt.dos, rt.mem
    vga = dos.vga
    mem.data[:] = mem_bytes
    for i in range(4):
        vga.planes[i][:] = planes_bytes[i * 0x10000:(i + 1) * 0x10000]

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
    cpu._irq_decim = c.get("irq_decim", 0)     # IRQ-poll phase (older snaps: 0)

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
    # Restore the game-programmed INT 33h range (older snapshots lack it —
    # fall back to the driver default so they still load).
    dos.mouse_range = list(s.get("mouse_range", [0, 639, 0, 199]))
    dos.timer_period_instructions = s["timer_period_instructions"]
    dos._timer_next = s["timer_next"]
    dos._next_handle = s["next_handle"]
    if "pic" in s:
        dos.pic.imr, dos.pic.irr, dos.pic.isr = s["pic"]
    for f in dos.files.values():
        f.close()
    dos.files = {}
    for h, (name, pos, mode) in s["files"].items():
        f = open(name, mode if "b" in mode else mode + "b")
        f.seek(pos)
        dos.files[int(h)] = f

    sb_state = state.get("sb")
    if sb_state is not None:
        sb = dos.attach_sound_blaster(base=sb_state["base"], irq=sb_state["irq"],
                                      dma=sb_state["dma"])
        sb.restore_state(sb_state["state"])
        sb.rearm_after_restore()

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


def save_pm_snapshot(rt, directory: str | Path) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / STATE_FILE).write_text(json.dumps(capture_pm_state(rt)))
    (d / MEM_FILE).write_bytes(zlib.compress(bytes(rt.mem.data), 6))
    (d / PLANES_FILE).write_bytes(zlib.compress(b"".join(rt.dos.vga.planes), 6))
    return d


def load_pm_snapshot(exe_path: str | Path, directory: str | Path, *,
                     game_root: str | Path | None = None):
    from .runtime import create_pm_runtime

    d = Path(directory)
    state = json.loads((d / STATE_FILE).read_text())
    rt = create_pm_runtime(exe_path, game_root=game_root,
                           ram_bytes=state["mem_size"])
    apply_pm_state(rt, state,
                   zlib.decompress((d / MEM_FILE).read_bytes()),
                   zlib.decompress((d / PLANES_FILE).read_bytes()))
    return rt


def clone_pm_runtime(rt):
    """A fresh, fully independent PMRuntime with identical state.

    Built bare (no LE reload — the memory image is transferred wholesale) so
    it also works for runtimes that were never created from an EXE (tests).
    Open files are reopened on the clone at the same positions, so oracle
    file reads never move the live runtime's cursors.
    """
    from .cpu386 import CPU386, FlatMemory
    from .dos4gw import DOS4GWHost
    from .runtime import PMRuntime

    state = capture_pm_state(rt)
    mem = FlatMemory(size=rt.mem.size)
    cpu = CPU386(mem, eip=0, esp=0)
    dos = DOS4GWHost(mem, rt.dos.root)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    cpu.idt = dos.pm_vectors
    cpu.pending_irq = dos.pending_irq
    dos._cpu = cpu
    clone = PMRuntime(image=rt.image, cpu=cpu, dos=dos, mem=mem)
    apply_pm_state(clone, state, rt.mem.data, b"".join(rt.dos.vga.planes))
    return clone
