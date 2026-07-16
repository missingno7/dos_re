from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .cpu import CPU8086, HaltExecution, UnsupportedInstruction, CF, ZF, IF, TF
from .memory import EGA_APERTURE, EGA_PLANE_STRIDE, EGA_PLANE_WINDOW


def _dac8(v6: int) -> int:
    """Expand a 6-bit VGA DAC component to 8 bits the way real VGA does.

    ``v << 2`` alone maxes out at 252 (slightly dark); replicating the high bits
    with ``| (v >> 4)`` makes 63 -> 255 and matches hardware brightness.
    """
    v = v6 & 0x3F
    return (v << 2) | (v >> 4)


# US-layout PC/XT set-1 scancode -> (unshifted, shifted) ASCII, for the BIOS
# INT 09h keyboard translation (DOSMachine.bios_int9_keyboard).  Only keys the
# BIOS puts in its type-ahead buffer as ASCII appear here.
_BIOS_SCANCODE_ASCII: dict[int, tuple[str, str]] = {
    0x01: ("\x1b", "\x1b"),                                          # Esc
    0x02: ("1", "!"), 0x03: ("2", "@"), 0x04: ("3", "#"), 0x05: ("4", "$"),
    0x06: ("5", "%"), 0x07: ("6", "^"), 0x08: ("7", "&"), 0x09: ("8", "*"),
    0x0A: ("9", "("), 0x0B: ("0", ")"), 0x0C: ("-", "_"), 0x0D: ("=", "+"),
    0x0E: ("\b", "\b"), 0x0F: ("\t", "\t"),
    0x10: ("q", "Q"), 0x11: ("w", "W"), 0x12: ("e", "E"), 0x13: ("r", "R"),
    0x14: ("t", "T"), 0x15: ("y", "Y"), 0x16: ("u", "U"), 0x17: ("i", "I"),
    0x18: ("o", "O"), 0x19: ("p", "P"), 0x1A: ("[", "{"), 0x1B: ("]", "}"),
    0x1C: ("\r", "\r"),
    0x1E: ("a", "A"), 0x1F: ("s", "S"), 0x20: ("d", "D"), 0x21: ("f", "F"),
    0x22: ("g", "G"), 0x23: ("h", "H"), 0x24: ("j", "J"), 0x25: ("k", "K"),
    0x26: ("l", "L"), 0x27: (";", ":"), 0x28: ("'", '"'), 0x29: ("`", "~"),
    0x2B: ("\\", "|"),
    0x2C: ("z", "Z"), 0x2D: ("x", "X"), 0x2E: ("c", "C"), 0x2F: ("v", "V"),
    0x30: ("b", "B"), 0x31: ("n", "N"), 0x32: ("m", "M"), 0x33: (",", "<"),
    0x34: (".", ">"), 0x35: ("/", "?"), 0x39: (" ", " "),
}

# Scancodes the BIOS buffers as *extended* keys (AL=0, AH=scancode): the F-key
# row and the (NumLock-off) navigation cluster.  Menus read these via INT 16h.
_BIOS_EXTENDED_KEYS: frozenset[int] = frozenset({
    0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44,  # F1-F10
    0x57, 0x58,                                                   # F11, F12
    0x47, 0x48, 0x49, 0x4B, 0x4D, 0x4F, 0x50, 0x51, 0x52, 0x53,  # nav cluster
})


class ConsoleInputWouldBlock(Exception):
    """Raised when an interactive front-end wants DOS console input to wait."""


class UnmodeledPortRead(NotImplementedError):
    """A program read a port the hardware model does not know (strict mode only).

    By default such reads return 0 — the proven behaviour the source games ran
    under (their detection probes rely on benign defaults) — and are recorded in
    ``DOSMachine.unmodeled_port_reads``.  Setting ``dos.strict_ports = True``
    turns them into this loud failure for recovery/audit sessions."""


@dataclass
class FileHandle:
    path: Path
    data: bytearray
    pos: int = 0
    writable: bool = False


@dataclass
class DOSMachine:
    root: Path
    stdout: list[str] = field(default_factory=list)
    files: dict[int, FileHandle] = field(default_factory=dict)
    next_handle: int = 5
    # Minimal DOS heap allocator.  Earlier scaffolding returned 7000h for
    # every AH=48h call, which was good enough for bootstrap probing but made
    # later source/image/work buffers alias each other.  Keep this
    # intentionally simple and deterministic: allocate paragraph blocks from
    # below VGA memory and remember sizes for snapshots/audits.
    next_alloc_segment: int = 0x7000
    allocation_limit_segment: int = 0xA000
    allocations: dict[int, int] = field(default_factory=dict)
    video_mode: int = 3
    video_page: int = 0
    text_mode_active: bool = True
    # Minimal VGA DAC state used by mode 13h and VGA-aware text/intro effects.
    # Values are stored as 8-bit RGB for the presenter; port writes use the real
    # 6-bit DAC payload and are expanded by shifting left by two.
    vga_palette: list[tuple[int, int, int]] = field(default_factory=list)
    _dac_write_index: int = 0
    _dac_read_index: int = 0
    _dac_component: int = 0
    _dac_latch: list[int] = field(default_factory=list)
    cursor_row: int = 0
    cursor_col: int = 0
    ticks: int = 0
    vga_status_reads: int = 0
    _seq_index: int = 0  # last EGA sequencer index latched via 03C4h
    _crtc_index: int = 0  # last colour CRTC index latched via 03D4h/03B4h
    # VGA register files, stored so indexed data ports read back the written
    # value.  PRE2's "100% compatible VGA" probe writes a register then reads it
    # back; returning 0 (the old default) reads as "not real VGA".
    _seq_regs: dict[int, int] = field(default_factory=dict)
    _gc_index: int = 0
    _gc_regs: dict[int, int] = field(default_factory=dict)
    _crtc_regs: dict[int, int] = field(default_factory=dict)
    # Attribute controller (port 03C0h): a single port with an index/data flip-flop
    # reset to index mode by reading the input-status register (03DAh/03BAh). PRE2 uses
    # only the pel-panning register (0x13) for sub-byte horizontal scroll.
    _attr_index: int = 0
    _attr_flipflop: bool = False
    _attr_regs: dict[int, int] = field(default_factory=dict)
    _misc_output: int = 0xA3  # VGA Misc Output Register (03C2h write / 03CCh read)
    _pit_channel2_access: int = 3
    _pit_channel2_latch: int = 0
    _pit_channel2_write_low: bool = True
    pit_channel2_reload: int = 0
    # PIT channel 0 = the system timer that drives IRQ0/INT 08h.  Tracked exactly
    # like channel 2 so a front-end can read the rate the program *itself*
    # programmed (1193182 / reload) and fire INT 08h at that frequency — no
    # game-specific timing is baked in.
    _pit_channel0_access: int = 3
    _pit_channel0_latch: int = 0
    _pit_channel0_write_low: bool = True
    pit_channel0_reload: int = 0  # 0 => 0x10000, the BIOS default ~18.2 Hz
    # Pending bytes for a channel-0 "latch count value" readback (OUT 43h with
    # SC=00,RW=00), consumed low-byte-first by the next IN AL,40h reads. A
    # program that reads channel 0 directly (never through IRQ0) as a short
    # hardware delay loop needs this.
    _pit_channel0_read_latch: list[int] = field(default_factory=list)
    # Optional emulated-time source in seconds.  When set, the VGA input-status
    # vertical-retrace bit (03DAh/03BAh) advances with time at the display refresh
    # rate instead of toggling per read.  An interactive front-end sets it to
    # wall-clock so the program's own vsync/timer waits pace it to real time; left
    # None for headless/deterministic runs (per-read toggle preserved).
    time_source: Callable[[], float] | None = None
    # Fraction of each refresh period the vertical-retrace status bit reads *active*
    # (only used with a wall-clock time_source).  Real VGA asserts it for a narrow
    # vertical-blank/retrace pulse; a wide window lets a program's "wait until the
    # retrace bit is set" half-wait slip more than one frame through per refresh on a
    # fast host (PRE2's mode-select scroll runs ~2x fast at 0.28).  Default preserves
    # the historical 0.28; lower it (~0.05-0.08) for a realistic narrow pulse.
    vga_retrace_active_fraction: float = 0.28
    speaker_control: int = 0
    speaker_callback: Callable[[bool, float], None] | None = None
    adlib_callback: Callable[[int, int], None] | None = None
    # Narrow AdLib/OPL2 model.  Some optional AdLib drivers first perform
    # the standard YM3812 timer-status probe through ports 388h/389h.  DOSMachine
    # owns detection/status and emits register writes to an optional frontend
    # callback; an SDL audio backend can then render the exact original driver
    # stream without making the headless VM depend on an OPL library.
    opl_selected_register: int = 0
    opl_status: int = 0
    opl_registers: dict[int, int] = field(default_factory=dict)
    port_log: list[tuple[str, int, int, int]] = field(default_factory=list)
    # Optional emulated sound hardware (a Sound Blaster + its DMA channel) and the
    # master PIC.  Left None on the deterministic/headless path; an interactive
    # front-end enables them (see runtime.enable_sound_blaster) so the program's
    # own driver detects the card and streams PCM.
    sound_blaster: "object | None" = None
    pic: "object | None" = None
    # Pending BIOS keystrokes as 16-bit values (high byte = scan code, low byte =
    # ASCII).  An interactive front-end pushes keys here; when empty the runtime
    # keeps its previous deterministic headless behaviour.
    key_queue: list[int] = field(default_factory=list)
    # Extended keys (arrows, F-keys, etc.) reach the byte-oriented DOS console
    # reads (INT 21h AH=01h/07h/08h, which return one byte in AL) as a TWO-call
    # sequence: the first read returns AL=00h, the second returns the scan code.
    # When AH=07h pops an extended key_queue entry (low byte 0, high byte = scan
    # code) it stashes that scan code here so the game's next read gets it, per
    # real DOS.  Modeling only the leading 00h and discarding the scan code left
    # menus that navigate via AH=07h (SkyRoads) unable to see arrows at all --
    # they read 00h, call again for the scan code, and block forever.
    pending_console_scancode: int | None = None
    # Deterministic headless fallback for blocking console reads.  Interactive
    # front-ends can set this to None so AH=01h/07h/08h waits for a real key
    # instead of synthesizing Esc.
    console_input_fallback: int | None = 0x011B
    # Latest raw keyboard scan code presented on port 60h.  A front-end sets this
    # and then invokes the installed INT 9 handler (see dos_re.interrupts).
    current_scancode: int = 0
    # 8042 keyboard controller status register (port 64h) bit 0 (output buffer
    # full): set when a scan code is presented, cleared when port 60h is read.
    # Code that polls the controller directly instead of via an installed INT 9h
    # handler (e.g. before a game installs its own ISR) needs this to see new
    # input at all -- a game's intro proved this gap: with the OBF bit
    # unmodeled (always reading 0, the prior default), a "wait for keyboard data
    # ready" poll never progresses to read port 60h.
    kbd_output_buffer_full: bool = False
    # BIOS keyboard shift/toggle state, maintained by the BIOS INT 9 handler
    # (bios_int9_keyboard).  A game that installs its own INT 9 ISR and *chains*
    # to the previous (BIOS) handler relies on that handler translating scan
    # codes into the type-ahead buffer that INT 16h serves.
    kbd_shift: bool = False
    kbd_ctrl: bool = False
    kbd_alt: bool = False
    kbd_caps: bool = False
    # Microsoft mouse (INT 33h) state.  A front-end feeds it via set_mouse_norm;
    # left at rest on the headless/deterministic path.  mouse_range is the
    # program's own virtual coordinate box (set via AX=7/8), so the pointer stays
    # proportional whatever box the game picks (VGA Lemmings uses 0..319 x 0..199).
    mouse_x: int = 160
    mouse_y: int = 100
    mouse_buttons: int = 0
    mouse_range: list = field(default_factory=lambda: [0, 639, 0, 199])
    # Unmodeled-I/O policy (docs/hardware_support.md): reads from ports the model
    # does not know return 0 — the proven default both source games ran under.
    # Every such read is recorded here (capped) so probes are auditable, and the
    # opt-in ``strict_ports`` flag turns them into loud failures for recovery/
    # audit sessions where a silently-wrong 0 could hide behind "working" runs.
    strict_ports: bool = False
    unmodeled_port_reads: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.vga_palette:
            # Standard VGA mode-13-ish default: first 16 entries match EGA, the
            # rest form a deterministic grayscale ramp until the game programs
            # the real DAC through 03C8h/03C9h.
            ega = [
                (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
                (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
                (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
                (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
            ]
            self.vga_palette = ega + [(i, i, i) for i in range(16, 256)]

    def _text_base_segment(self) -> int:
        return 0xB000 if self.video_mode == 7 else 0xB800

    def _text_page_offset(self) -> int:
        return (self.video_page & 0x07) * 0x1000

    def _write_text_cell(self, cpu: CPU8086, row: int, col: int, ch: int, attr: int | None) -> None:
        row = max(0, min(24, row & 0xFF))
        col = max(0, min(79, col & 0xFF))
        off = self._text_page_offset() + ((row * 80 + col) * 2)
        base = self._text_base_segment()
        cpu.mem.wb(base, off & 0xFFFF, ch & 0xFF)
        if attr is not None:
            cpu.mem.wb(base, (off + 1) & 0xFFFF, attr & 0xFF)

    def _write_text_char(self, cpu: CPU8086, ch: int, attr: int = 0x07) -> None:
        ch &= 0xFF
        if ch == 0x07:  # bell
            self.stdout.append(chr(ch))
            return
        if ch == 0x08:  # backspace
            self.cursor_col = max(0, self.cursor_col - 1)
            return
        if ch == 0x0D:
            self.cursor_col = 0
            return
        if ch == 0x0A:
            self.cursor_row = min(24, self.cursor_row + 1)
            return

        self._write_text_cell(cpu, self.cursor_row, self.cursor_col, ch, attr)
        self.cursor_col += 1
        if self.cursor_col >= 80:
            self.cursor_col = 0
            self.cursor_row = min(24, self.cursor_row + 1)

    def _write_text_repeat(self, cpu: CPU8086, ch: int, attr: int | None, count: int) -> None:
        row = self.cursor_row
        col = self.cursor_col
        for _ in range(count):
            self._write_text_cell(cpu, row, col, ch, attr)
            col += 1
            if col >= 80:
                col = 0
                row = min(24, row + 1)

    def _console_output(self, cpu: CPU8086, text: str) -> None:
        self.stdout.append(text)
        if self.text_mode_active and self.video_mode in (0, 1, 2, 3, 7):
            for ch in text:
                if ch != "\x07":
                    self._write_text_char(cpu, ord(ch), 0x07)

    def _clear_text_window(self, cpu: CPU8086, attr: int, top: int, left: int, bottom: int, right: int) -> None:
        top = max(0, min(24, top & 0xFF))
        bottom = max(0, min(24, bottom & 0xFF))
        left = max(0, min(79, left & 0xFF))
        right = max(0, min(79, right & 0xFF))
        if bottom < top or right < left:
            return
        base = self._text_base_segment()
        page = self._text_page_offset()
        for row in range(top, bottom + 1):
            off = page + ((row * 80 + left) * 2)
            for _ in range(left, right + 1):
                cpu.mem.wb(base, off & 0xFFFF, 0x20)
                cpu.mem.wb(base, (off + 1) & 0xFFFF, attr & 0xFF)
                off += 2

    def _clear_graphics_vram_for_mode(self, cpu: CPU8086, mode: int) -> None:
        """Model BIOS mode-set screen clearing for common graphics modes.

        Real BIOS mode sets clear display memory unless AL bit 7 requests "no
        clear".  The VM mirrors this for the common graphics modes so that stale
        bytes left in video memory by a previous mode (e.g. text cells written to
        B800h) are not reinterpreted by the next mode's frame decoder.
        """
        if mode & 0x80:
            return
        mode &= 0x7F
        if mode in (0x0D, 0x0E, 0x10, 0x12):
            # EGA/VGA *planar* modes: the displayed pixels live in the four shadow
            # planes (EGA_APERTURE), NOT in the legacy 0xA0000 aperture (which is
            # only the CPU write window routed through the planar latches). A real
            # BIOS mode-set clears the planar display memory, so we must zero the
            # shadow planes — clearing 0xA0000 here was a no-op for planar pixels,
            # which left the previous screen visible after a mode transition.
            for plane in range(4):
                base = EGA_APERTURE + plane * EGA_PLANE_STRIDE
                cpu.mem.data[base:base + EGA_PLANE_WINDOW] = b"\x00" * EGA_PLANE_WINDOW
            return
        if mode in (0x04, 0x05, 0x06):
            start, size = 0xB8000, 0x4000
        elif mode == 0x09:
            start, size = 0xB8000, 0x8000
        elif mode in (0x13, 0x19):
            start, size = 0xA0000, 0x10000
        else:
            return
        cpu.mem.data[start:start + size] = b"\x00" * size

    def set_speaker_callback(self, callback: Callable[[bool, float], None] | None, *, emit_current: bool = False) -> None:
        """Install a PC-speaker observer, optionally emitting the current state.

        Runtime snapshots can be taken while a tone is already active.  An
        observer attached after ``load_snapshot`` restores DOS state needs one
        immediate notification; otherwise the next port write is the first
        audible event and an already-playing tone is lost.
        """
        self.speaker_callback = callback
        if emit_current and callback is not None:
            self._notify_speaker()

    def set_adlib_callback(self, callback: Callable[[int, int], None] | None, *, emit_current: bool = False) -> None:
        """Install an AdLib register-write observer.

        Original optional AdLib drivers may write to YM3812 ports
        388h/389h from the loaded sound module at 2032:0000.  The core DOS layer
        keeps the detection/status model deterministic and forwards completed
        data-port writes to the interactive frontend, where an optional Nuked-OPL3
        backend can synthesize them.  Snapshots can replay final register state
        on attach; exact historical write timing resumes from new writes.
        """
        self.adlib_callback = callback
        if emit_current and callback is not None:
            for reg, value in sorted(self.opl_registers.items()):
                callback(reg & 0x1FF, value & 0xFF)

    def _notify_adlib(self, reg: int, value: int) -> None:
        if self.adlib_callback is not None:
            self.adlib_callback(reg & 0x1FF, value & 0xFF)



    def seed_initial_memory_block(self, psp_segment: int, top_segment: int = 0xA000) -> None:
        """Register the DOS-owned initial PSP memory block.

        A real DOS process starts with one allocation whose owner is the PSP.
        Packed DOS games often shrink that block with INT 21h/AH=4Ah before
        requesting its own buffers with AH=48h.  Modelling that block avoids
        treating the shrink as an error while still keeping the allocator
        narrow and deterministic.
        """
        psp = psp_segment & 0xFFFF
        top = top_segment & 0xFFFF
        if top <= psp:
            raise ValueError(f"invalid DOS memory block {psp:04X}..{top:04X}")
        self.allocation_limit_segment = top
        self.allocations[psp] = top - psp
        self.next_alloc_segment = top

    def read_asciiz(self, cpu: CPU8086, seg: int, off: int, limit: int = 260) -> str:
        bs = bytearray()
        for i in range(limit):
            b = cpu.mem.rb(seg, (off + i) & 0xFFFF)
            if b == 0:
                break
            bs.append(b)
        return bs.decode("cp437", errors="replace")

    def read_dollar_string(self, cpu: CPU8086, seg: int, off: int, limit: int = 4096) -> str:
        bs = bytearray()
        for i in range(limit):
            b = cpu.mem.rb(seg, (off + i) & 0xFFFF)
            if b == ord("$"):
                break
            bs.append(b)
        return bs.decode("cp437", errors="replace")

    def resolve_game_path(self, name: str) -> Path:
        # DOS paths are often relative and uppercase. Keep this intentionally narrow.
        clean = name.replace("\\", "/").strip().lstrip("/")
        direct = self.root / clean
        if direct.exists():
            return direct
        target = clean.upper()
        for p in self.root.rglob("*"):
            if str(p.relative_to(self.root)).replace("/", "\\").upper() == target.replace("/", "\\"):
                return p
            if p.name.upper() == Path(clean).name.upper():
                return p
        return direct


    # PIT channel 0 (IRQ0/INT 08h) frequency the program itself programmed.
    PIT_INPUT_HZ = 1193182.0
    # Coarse period-accurate stand-in for real 8088 instruction timing (~4.77 MHz,
    # a handful of clocks/instruction) used only to age the channel-0 down-counter
    # deterministically when no wall-clock time_source is set -- see
    # _pit_channel0_live_value. Not a cycle-exact CPU model; bounded, reproducible
    # progress for direct-read delay loops is all that is required here.
    PIT_TICKS_PER_INSTRUCTION_ESTIMATE = 3.0

    def pit_channel0_hz(self) -> float:
        return self.PIT_INPUT_HZ / (self.pit_channel0_reload or 0x10000)

    def _pit_channel0_live_value(self, cpu: CPU8086) -> int:
        """The channel-0 down-counter's current value (mode 3 free-running square wave)."""
        reload = self.pit_channel0_reload or 0x10000
        ts = self.time_source
        if ts is not None:
            elapsed_ticks = int(ts() * self.PIT_INPUT_HZ)
        else:
            elapsed_ticks = int(cpu.instruction_count * self.PIT_TICKS_PER_INSTRUCTION_ESTIMATE)
        return (reload - (elapsed_ticks % reload)) % reload

    def display_refresh_hz(self) -> float:
        # VGA 320x200 graphics and text modes refresh at ~70 Hz; a property of the
        # CRTC timing, not of any particular program.
        return 70.0

    def _vga_status(self, retrace_bit: int) -> int:
        # Reading the input-status register resets the attribute-controller flip-flop to
        # index mode (the real hardware behaviour PRE2 relies on before writing the pel pan).
        self._attr_flipflop = False
        # VGA input status register 1. The passed bit reflects vertical retrace.
        # With a time source it advances at the display refresh rate (so the
        # program's own vsync waits run at real speed); otherwise it toggles per
        # read so busy-wait loops still make progress in deterministic runs.
        self.vga_status_reads += 1
        # Bit 0 = Display Enable ("not displaying": set during any blank interval).
        # On hardware it flips every scan line, far faster than vertical retrace.
        # Per-scan-line timing loops poll it (VGA Lemmings waits on it 60x to time
        # a delay before loading the level palette); without it toggling, that
        # loop never exits and the game hangs on a black screen.  Toggle it every
        # read so those loops make progress, independent of the retrace phase.
        display_enable = 0x01 if (self.vga_status_reads & 1) else 0x00
        ts = self.time_source
        if ts is None:
            retrace = retrace_bit if (self.vga_status_reads & 1) else 0x00
        else:
            phase = (ts() * self.display_refresh_hz()) % 1.0
            retrace = retrace_bit if phase >= (1.0 - self.vga_retrace_active_fraction) else 0x00
        return retrace | display_enable

    def port_read(self, cpu: CPU8086, port: int, bits: int) -> int:
        sb = self.sound_blaster
        if sb is not None and bits == 8:
            if sb.owns_port(port):
                return sb.port_read(port)
            if port == 0x21 and self.pic is not None:
                return self.pic.get_mask()
        # VGA input status register 1. Bit 3 is vertical retrace.
        if port == 0x03BA and bits == 8:
            return self._vga_status(0x80)
        if port == 0x03DA and bits == 8:
            return self._vga_status(0x08)
        if port == 0x60 and bits == 8:
            # 8042 keyboard data port: the game's INT 9 handler (or code polling
            # the controller directly) reads the scan code here. Reading clears
            # the output-buffer-full status bit (port 64h), matching real hardware.
            self.kbd_output_buffer_full = False
            return self.current_scancode & 0xFF
        if port == 0x64 and bits == 8:
            # 8042 keyboard controller status register. Only bit 0 (output
            # buffer full) is modeled; the other status bits (input buffer
            # full, self-test, etc.) are not exercised by a program that only
            # polls for "is a scan code waiting".
            return 0x01 if self.kbd_output_buffer_full else 0x00
        if port == 0x40 and bits == 8:
            # PIT channel 0 data port. A prior "latch count" command (OUT 43h,
            # SC=00 RW=00) queues [lo, hi] of the down-counter at latch time;
            # consume it low-byte-first. With no pending latch (a bare,
            # unlatched read), fall back to the live counter's low byte.
            if self._pit_channel0_read_latch:
                return self._pit_channel0_read_latch.pop(0)
            return self._pit_channel0_live_value(cpu) & 0xFF
        if port == 0x61 and bits == 8:
            return self.speaker_control & 0xFF
        if port == 0x03C6 and bits == 8:
            # VGA DAC pixel mask.  PRE2's VGA compatibility probe expects the
            # normal unmasked value.
            return 0xFF
        if port == 0x03C9 and bits == 8:
            # VGA DAC data read.  After OUT 03C7h,index, reads from 03C9h return
            # R/G/B 6-bit components and then advance to the next palette index.
            if len(self.vga_palette) < 256:
                self.__post_init__()
            idx = self._dac_read_index & 0xFF
            comp = self._dac_component % 3
            value = (self.vga_palette[idx][comp] >> 2) & 0x3F
            self._dac_component = (self._dac_component + 1) % 3
            if self._dac_component == 0:
                self._dac_read_index = (idx + 1) & 0xFF
            return value
        if port in (0x388, 0x389) and bits == 8:
            # AdLib/YM3812 status port.  Only the timer status bits are needed
            # for startup detection; register reads are not used.
            return self.opl_status & 0xFF
        # VGA indexed-register read-back: return the value last written to the
        # currently latched index so PRE2's VGA-compatibility probe sees real
        # register behaviour instead of zeros.
        if port == 0x03C5 and bits == 8:  # sequencer data
            return self._seq_regs.get(self._seq_index & 0xFF, 0) & 0xFF
        if port == 0x03CF and bits == 8:  # graphics controller data
            return self._gc_regs.get(self._gc_index & 0xFF, 0) & 0xFF
        if port in (0x03D5, 0x03B5) and bits == 8:  # CRTC data
            return self._crtc_regs.get(self._crtc_index & 0xFF, 0) & 0xFF
        if port == 0x03CC and bits == 8:  # Misc Output read-back
            return self._misc_output & 0xFF
        # Unmodeled port: record it, and fail loud when a recovery/audit session
        # opted in (a program whose LOGIC consumes this read would silently get a
        # wrong 0 — the exact fake-confidence hazard the strict mode exists for).
        if len(self.unmodeled_port_reads) < 512:
            self.unmodeled_port_reads.append((port & 0xFFFF, bits))
        if self.strict_ports:
            raise UnmodeledPortRead(
                f"read from unmodeled port {port:04X}h ({bits}-bit) at "
                f"{cpu.s.cs:04X}:{cpu.s.ip:04X} -- model the observed behaviour "
                f"(see docs/hardware_support.md) or run without strict_ports"
            )
        return 0

    def port_write(self, cpu: CPU8086, port: int, value: int, bits: int) -> None:
        if len(self.port_log) < 4096:
            self.port_log.append(("out", port & 0xFFFF, value & ((1 << bits) - 1), bits))
        port &= 0xFFFF
        if self.sound_blaster is not None and bits == 8 and self._route_sound_write(port, value):
            return
        self._track_pc_speaker(cpu, port, value, bits)
        self._track_vga_dac_ports(port, value, bits)
        self._track_ega_ports(cpu, port, value, bits)
        self._track_adlib_ports(port, value, bits)

    def _route_sound_write(self, port: int, value: int) -> bool:
        """Route a byte write to the Sound Blaster / its DMA channel / the PIC.

        Returns True if it was a sound/DMA/PIC port (the PIT at 0x40-0x43 and the
        speaker at 0x61 stay with _track_pc_speaker).
        """
        sb = self.sound_blaster
        if sb.owns_port(port):
            sb.port_write(port, value)
            return True
        if port <= 0x0F:                    # 8237 DMA controller #1
            sb.dma_controller_write(port, value)
            return True
        if 0x80 <= port <= 0x8F:            # DMA page registers
            sb.page_write(port, value)
            return True
        if self.pic is not None:
            if port == 0x20:                # PIC command (non-specific EOI = 0x20)
                if value == 0x20:
                    self.pic.eoi()
                return True
            if port == 0x21:                # PIC mask
                self.pic.set_mask(value)
                return True
        return False

    def _track_vga_dac_ports(self, port: int, value: int, bits: int) -> None:
        if bits == 16:
            self._track_vga_dac_ports(port, value & 0xFF, 8)
            self._track_vga_dac_ports((port + 1) & 0xFFFF, (value >> 8) & 0xFF, 8)
            return
        if bits != 8:
            return
        value &= 0xFF
        if port == 0x03C8:
            self._dac_write_index = value & 0xFF
            self._dac_component = 0
            self._dac_latch = []
            return
        if port == 0x03C7:
            self._dac_read_index = value & 0xFF
            self._dac_component = 0
            return
        if port != 0x03C9:
            return
        self._dac_latch.append(_dac8(value))
        self._dac_component += 1
        if self._dac_component >= 3:
            r, g, b = (self._dac_latch + [0, 0, 0])[:3]
            idx = self._dac_write_index & 0xFF
            if len(self.vga_palette) < 256:
                self.__post_init__()
            self.vga_palette[idx] = (r, g, b)
            self._dac_write_index = (idx + 1) & 0xFF
            self._dac_component = 0
            self._dac_latch = []

    def _track_adlib_ports(self, port: int, value: int, bits: int) -> None:
        if bits == 16:
            self._track_adlib_ports(port, value & 0xFF, 8)
            self._track_adlib_ports((port + 1) & 0xFFFF, (value >> 8) & 0xFF, 8)
            return
        if bits != 8:
            return

        value &= 0xFF
        if port == 0x388:
            self.opl_selected_register = value
            return
        if port != 0x389:
            return

        reg = self.opl_selected_register & 0xFF
        self.opl_registers[reg] = value
        self._notify_adlib(reg, value)
        if reg == 0x04:
            # YM3812 timer-control/status approximation:
            #   bit 7 resets/clears timer flags;
            #   bit 0 starts timer 1.
            # The classic AdLib presence test programs timer 1 then expects
            # status bits 7 and 6 to become set after a short delay.  Model the
            # expiration eagerly; the VM does not advance real device time.
            if value & 0x80:
                self.opl_status = 0
            elif value & 0x01:
                self.opl_status = 0xC0

    def _track_pc_speaker(self, cpu: CPU8086, port: int, value: int, bits: int) -> None:
        if bits == 16:
            self._track_pc_speaker(cpu, port, value & 0xFF, 8)
            self._track_pc_speaker(cpu, (port + 1) & 0xFFFF, (value >> 8) & 0xFF, 8)
            return

        value &= 0xFF
        if port == 0x43:
            channel = (value >> 6) & 0x03
            access = (value >> 4) & 0x03
            if channel == 2:
                self._pit_channel2_access = access
                self._pit_channel2_write_low = True
                if access in (1, 2):
                    self._pit_channel2_latch = 0
            elif channel == 0:
                if access == 0:
                    # Counter-latch command: snapshot the live down-counter for
                    # the next IN AL,40h reads. Real 8253 latch commands do not
                    # disturb the counter's programmed read/write access mode.
                    counter = self._pit_channel0_live_value(cpu)
                    self._pit_channel0_read_latch = [counter & 0xFF, (counter >> 8) & 0xFF]
                else:
                    self._pit_channel0_access = access
                    self._pit_channel0_write_low = True
                    if access in (1, 2):
                        self._pit_channel0_latch = 0
            return
        if port == 0x40:
            access = self._pit_channel0_access
            if access == 1:
                self.pit_channel0_reload = (self.pit_channel0_reload & 0xFF00) | value
            elif access == 2:
                self.pit_channel0_reload = (self.pit_channel0_reload & 0x00FF) | (value << 8)
            else:
                if self._pit_channel0_write_low:
                    self._pit_channel0_latch = value
                    self._pit_channel0_write_low = False
                else:
                    self.pit_channel0_reload = ((value << 8) | self._pit_channel0_latch) & 0xFFFF
                    self._pit_channel0_write_low = True
            return
        if port == 0x42:
            access = self._pit_channel2_access
            if access == 1:
                self.pit_channel2_reload = (self.pit_channel2_reload & 0xFF00) | value
                self._notify_speaker()
            elif access == 2:
                self.pit_channel2_reload = (self.pit_channel2_reload & 0x00FF) | (value << 8)
                self._notify_speaker()
            else:
                if self._pit_channel2_write_low:
                    self._pit_channel2_latch = value
                    self._pit_channel2_write_low = False
                else:
                    self.pit_channel2_reload = ((value << 8) | self._pit_channel2_latch) & 0xFFFF
                    self._pit_channel2_write_low = True
                    self._notify_speaker()
            return
        if port == 0x61:
            self.speaker_control = value
            self._notify_speaker()

    def _notify_speaker(self) -> None:
        if self.speaker_callback is None:
            return
        reload = self.pit_channel2_reload or 0x10000
        enabled = (self.speaker_control & 0x03) == 0x03 and reload != 0
        freq = 1193182.0 / reload if enabled else 0.0
        self.speaker_callback(enabled, freq)

    def _apply_gc_register(self, mem, index: int, data: int) -> None:
        """Apply one Graphics Controller register write to the memory model."""
        index &= 0xFF
        data &= 0xFF
        if index == 0x00:        # Set/Reset (per-plane constant colour for write mode 0)
            mem.ega_set_reset = data & 0x0F
        elif index == 0x01:      # Enable Set/Reset (per-plane: use Set/Reset vs CPU data)
            mem.ega_enable_set_reset = data & 0x0F
        elif index == 0x08:      # Bit Mask (per-bit: ALU result vs preserved latch bit)
            mem.ega_bit_mask = data & 0xFF
        elif index == 0x02:      # Color Compare
            mem.ega_color_compare = data & 0x0F
        elif index == 0x03:      # Data Rotate / Function Select
            mem.ega_data_rotate = data & 0x07
            mem.ega_logical_op = (data >> 3) & 0x03
        elif index == 0x04:      # Read Map Select
            mem.ega_read_plane = data & 0x03
        elif index == 0x05:      # Graphics Mode (write mode 0-1, read mode bit 3)
            mem.ega_write_mode = data & 0x03
            mem.ega_read_mode = (data >> 3) & 0x01
        elif index == 0x07:      # Color Don't Care
            mem.ega_color_dont_care = data & 0x0F

    def _track_ega_ports(self, cpu: CPU8086, port: int, value: int, bits: int) -> None:
        # Track just enough EGA sequencer state to drive planar A000h writes (see
        # Memory.ega_planar).  The game programs the map-mask register at 03C4h
        # index 02h, either as two byte OUTs (index then data) or a single 16-bit
        # OUT where AL=index and AH=data.  Touching the sequencer at all means we
        # are in EGA mode, so enable planar routing here.
        mem = cpu.mem
        planar_allowed = (self.video_mode & 0x7F) not in (0x13, 0x19)
        if port == 0x3C2:
            # Miscellaneous Output Register (write side; read back at 03CCh).
            self._misc_output = value & 0xFF
        elif port == 0x3C0:
            # Attribute controller: index/data flip-flop on one port (reset to index mode
            # by reading 03DAh). The only register PRE2 uses for scrolling is the pel-panning
            # register (0x13) — the sub-byte (0-7 px) fine horizontal pan the CRTC start can't
            # express. Bit 5 of the index byte is the palette-address-source flag (ignored here).
            if not self._attr_flipflop:
                self._attr_index = value & 0x1F
                # Bit 5 = Palette Address Source: 1 = display on (palette locked), 0 = display blanked
                # (palette being loaded). The program clears it to load a new palette, then sets it.
                mem.ega_display_enabled = bool(value & 0x20)
                self._attr_flipflop = True
            else:
                self._attr_regs[self._attr_index] = value & 0xFF
                if self._attr_index == 0x13:
                    mem.ega_pel_pan = value & 0x0F
                    # Latch the display origin (start + pel) together. The program sets the CRTC
                    # start, waits for vsync, then writes the pel pan, so at this instant both belong
                    # to the same frame — snapshotting them here gives the present a tear-free pair.
                    mem.ega_pan_display_start = mem.ega_display_start
                    mem.ega_pan_pel = value & 0x0F
                    mem.ega_pan_active = True
                self._attr_flipflop = False
        elif port == 0x3C4:
            if planar_allowed:
                mem.ega_planar = True
            if bits == 16:
                self._seq_index = value & 0xFF
                self._seq_regs[value & 0xFF] = (value >> 8) & 0xFF
                if (value & 0xFF) == 0x02:
                    mem.ega_map_mask = (value >> 8) & 0x0F
            else:
                self._seq_index = value & 0xFF
        elif port == 0x3C5:
            if planar_allowed:
                mem.ega_planar = True
            self._seq_regs[self._seq_index & 0xFF] = value & 0xFF
            if getattr(self, "_seq_index", None) == 0x02:
                mem.ega_map_mask = value & 0x0F
        elif port == 0x3CE:
            if planar_allowed:
                mem.ega_planar = True
            if bits == 16:
                index = value & 0xFF
                data = (value >> 8) & 0xFF
                self._gc_index = index
                self._gc_regs[index] = data
                self._apply_gc_register(mem, index, data)
            else:
                self._gc_index = value & 0xFF
        elif port == 0x3CF:
            if planar_allowed:
                mem.ega_planar = True
            self._gc_regs[self._gc_index & 0xFF] = value & 0xFF
            self._apply_gc_register(mem, getattr(self, "_gc_index", 0), value & 0xFF)
        elif port in (0x3D4, 0x3B4):
            if bits == 16:
                index = value & 0xFF
                data = (value >> 8) & 0xFF
                self._crtc_index = index
                self._crtc_regs[index] = data
                self._write_crtc_register(mem, index, data)
            else:
                self._crtc_index = value & 0xFF
        elif port in (0x3D5, 0x3B5):
            self._crtc_regs[self._crtc_index & 0xFF] = value & 0xFF
            self._write_crtc_register(mem, getattr(self, "_crtc_index", 0), value & 0xFF)

    def _write_crtc_register(self, mem, index: int, value: int) -> None:
        index &= 0xFF
        value &= 0xFF
        if index == 0x0C:
            mem.ega_display_start = ((value << 8) | (mem.ega_display_start & 0x00FF)) & 0xFFFF
        elif index == 0x0D:
            mem.ega_display_start = ((mem.ega_display_start & 0xFF00) | value) & 0xFFFF
        elif index == 0x01:
            # Horizontal Display End: last active character index. Active width = (value+1)*8 px.
            # PRE2's carte sets 38 (312px) so the pel-pan overflow stays in the border.
            mem.ega_h_display_end = value

    def interrupt(self, cpu: CPU8086, num: int) -> None:
        if num == 0x20:
            cpu.halted = True
            raise HaltExecution()
        if num == 0x21:
            self.int21(cpu)
            return
        if num == 0x10:
            self.int10(cpu)
            return
        if num == 0x11:  # BIOS equipment list
            cpu.s.ax = 0x0020  # EGA/VGA-style display, no exotic peripherals
            return
        if num == 0x12:  # conventional memory size in KB
            cpu.s.ax = 640
            return
        if num == 0x15:
            self.int15(cpu)
            return
        if num == 0x16:
            self.int16(cpu)
            return
        if num == 0x1A:
            self.int1a(cpu)
            return
        if num == 0x2F:
            self.int2f(cpu)
            return
        if num == 0x33:
            self.int33(cpu)
            return
        if num == 0x67:
            self.int67(cpu)
            return
        # Not a framework-emulated BIOS/DOS service.  If the program installed
        # its OWN handler in the IVT — e.g. a game sound driver on the user
        # vectors INT 60h/61h (VGA Lemmings does exactly this) — dispatch to it
        # exactly as an `int` instruction does on hardware, and let it run to its
        # iret.  Only a genuinely unset vector (0000:0000) is a real gap worth
        # failing loud on.
        off = cpu.mem.rw(0, (num * 4) & 0xFFFFF)
        seg = cpu.mem.rw(0, (num * 4 + 2) & 0xFFFFF)
        if seg or off:
            cpu.push(cpu.s.flags)
            cpu.push(cpu.s.cs & 0xFFFF)
            cpu.push(cpu.s.ip & 0xFFFF)
            cpu.set_flag(IF, False)
            cpu.set_flag(TF, False)
            cpu.s.cs, cpu.s.ip = seg & 0xFFFF, off & 0xFFFF
            return
        raise UnsupportedInstruction(f"Unhandled interrupt INT {num:02X}h at {cpu.s.cs:04X}:{cpu.s.ip:04X}")

    def _alloc_handle(self) -> int:
        """Return the lowest free DOS file handle (>= 5, after the five standard
        handles).  Real DOS reuses the lowest closed handle; a monotonically
        increasing counter instead lets handles climb without bound, and a game
        that indexes a fixed-size per-handle table by the handle value will then
        write out of bounds (Ancient Empires' handle table at DS:38CC overruns
        into its text-colour table at DS:3904 once a handle reaches 28 — every
        menu drew red instead of black)."""
        handle = 5
        while handle in self.files:
            handle += 1
        self.next_handle = handle + 1  # kept for compatibility/inspection
        return handle

    def int21(self, cpu: CPU8086) -> None:
        ah = (cpu.s.ax >> 8) & 0xFF
        al = cpu.s.ax & 0xFF
        if ah == 0x00 or ah == 0x4C:
            cpu.halted = True
            raise HaltExecution()
        if ah == 0x09:
            text = self.read_dollar_string(cpu, cpu.s.ds, cpu.s.dx)
            self._console_output(cpu, text)
            cpu.s.ax = (cpu.s.ax & 0xFF00) | ord("$")
            return
        if ah == 0x02:
            self._console_output(cpu, chr(cpu.s.dx & 0xFF))
            return
        if ah in (0x01, 0x07, 0x08):
            # Console character input.
            #
            # AH=01h: wait for character, echo, Ctrl-C checked by DOS.
            # AH=07h: direct character input, no echo, no Ctrl-C check.
            # AH=08h: character input, no echo, Ctrl-C checked by DOS.
            #
            # This emulator does not have a real DOS stdin stream.  Use the same
            # deterministic keyboard queue as INT 16h.  These calls return ONE
            # byte in AL; an extended key (arrow/F-key) is delivered as AL=00h
            # then, on the next call, the scan code -- so a pending scan code
            # from a prior call is returned first, before touching the queue.
            if self.pending_console_scancode is not None:
                ch = self.pending_console_scancode & 0xFF
                self.pending_console_scancode = None
            elif self.key_queue:
                key = self.key_queue.pop(0)
                ch = key & 0xFF
                if ch == 0 and (key >> 8):
                    # Extended key: return 00h now, stash the scan code for the
                    # game's follow-up read (real DOS two-call behaviour).
                    self.pending_console_scancode = (key >> 8) & 0xFF
            elif self.console_input_fallback is not None:
                ch = self.console_input_fallback & 0xFF
            else:
                cpu.s.ip = (cpu.s.ip - 2) & 0xFFFF
                raise ConsoleInputWouldBlock()
            cpu.s.ax = (cpu.s.ax & 0xFF00) | ch
            if ah == 0x01:
                self._console_output(cpu, chr(ch))
            return
        if ah == 0x0B:  # check stdin input status: AL=FFh if a char is ready, else 00h
            ready = self.pending_console_scancode is not None or bool(self.key_queue)
            cpu.s.ax = (cpu.s.ax & 0xFF00) | (0xFF if ready else 0x00)
            return
        if ah == 0x30:  # get DOS version
            cpu.s.ax = 0x0005
            cpu.s.bx = 0x0000
            cpu.s.cx = 0x0000
            cpu.set_flag(CF, False)
            return
        if ah == 0x35:  # get interrupt vector AL -> ES:BX
            vec = al & 0xFF
            cpu.s.bx = cpu.mem.rw(0, vec * 4)
            cpu.s.es = cpu.mem.rw(0, vec * 4 + 2)
            return
        if ah == 0x25:  # set interrupt vector AL = DS:DX (write the real IVT)
            vec = al & 0xFF
            cpu.mem.ww(0, vec * 4, cpu.s.dx)
            cpu.mem.ww(0, vec * 4 + 2, cpu.s.ds)
            return
        if ah == 0x19:  # get current default drive (0=A, 2=C). Return C:.
            cpu.s.ax = (cpu.s.ax & 0xFF00) | 2
            return
        if ah == 0x1A:  # set DTA
            return
        if ah == 0x1B:  # get allocation info for the default drive
            # Returns AL=sectors per cluster, CX=bytes per sector, DX=total
            # clusters, and DS:BX -> the drive's media descriptor byte.  Games
            # call it as a lightweight "is there a disk / what media" probe; the
            # only value observed being consumed is the media byte at DS:BX
            # (VGA Lemmings reads [BX] and stores it, ignoring AL/CX/DX).  We
            # publish the media byte into a fixed low-memory scratch cell the
            # loader never allocates (linear 0500h, the DOS data area) so the
            # returned pointer is always valid and deterministic.
            scratch_seg, scratch_off = 0x0050, 0x0000
            cpu.mem.wb(scratch_seg, scratch_off, 0xF8)  # F8h = fixed disk
            cpu.s.ax = (cpu.s.ax & 0xFF00) | 0x04       # sectors per cluster
            cpu.s.cx = 512                              # bytes per sector
            cpu.s.dx = 0x8000                           # total clusters
            cpu.s.ds = scratch_seg
            cpu.s.bx = scratch_off
            cpu.set_flag(CF, False)
            return
        if ah == 0x43:  # get/set file attributes
            # AL=00h get: CF=0 with CX=attributes if the file exists, else
            # CF=1/AX=2 (file not found).  AL=01h set: accept and report
            # success (RE runs never mutate the user's game directory).  VGA
            # Lemmings uses AL=00h purely as an existence probe (e.g. adlib.dat
            # to decide whether Adlib sound is available), branching on CF.
            if al == 0x00:
                name = self.read_asciiz(cpu, cpu.s.ds, cpu.s.dx)
                if self.resolve_game_path(name).is_file():
                    cpu.s.cx = 0x20                     # archive bit set (normal file)
                    cpu.set_flag(CF, False)
                else:
                    cpu.s.ax = 2
                    cpu.set_flag(CF, True)
            else:
                cpu.set_flag(CF, False)
            return
        if ah == 0x3C:  # create/truncate file
            name = self.read_asciiz(cpu, cpu.s.ds, cpu.s.dx)
            path = self.resolve_game_path(name)
            handle = self._alloc_handle()
            # Keep writes in-memory so RE runs are deterministic and do not
            # mutate the user's original game directory.
            self.files[handle] = FileHandle(path, bytearray(), pos=0, writable=True)
            cpu.s.ax = handle
            cpu.set_flag(CF, False)
            return
        if ah == 0x3D:  # open file
            name = self.read_asciiz(cpu, cpu.s.ds, cpu.s.dx)
            path = self.resolve_game_path(name)
            if not path.is_file():        # missing, OR an empty/invalid name that resolved to a directory
                cpu.s.ax = 2              # DOS "file not found" (CF=1) -> the game's open-fail path, not a crash
                cpu.set_flag(CF, True)
                return
            handle = self._alloc_handle()
            self.files[handle] = FileHandle(path, bytearray(path.read_bytes()))
            cpu.s.ax = handle
            cpu.set_flag(CF, False)
            return
        if ah == 0x3E:  # close
            self.files.pop(cpu.s.bx, None)
            cpu.set_flag(CF, False)
            return
        if ah == 0x3F:  # read
            h = self.files.get(cpu.s.bx)
            if h is None:
                cpu.s.ax = 6
                cpu.set_flag(CF, True)
                return
            n = min(cpu.s.cx, len(h.data) - h.pos)
            for i in range(n):
                cpu.mem.wb(cpu.s.ds, (cpu.s.dx + i) & 0xFFFF, h.data[h.pos + i])
            h.pos += n
            cpu.s.ax = n
            cpu.set_flag(CF, False)
            return
        if ah == 0x40:  # write
            data = cpu.mem.block(cpu.s.ds, cpu.s.dx, cpu.s.cx)
            if cpu.s.bx in (1, 2):
                self._console_output(cpu, data.decode("cp437", errors="replace"))
                cpu.s.ax = cpu.s.cx
                cpu.set_flag(CF, False)
                return
            h = self.files.get(cpu.s.bx)
            if h is None:
                cpu.s.ax = 6
                cpu.set_flag(CF, True)
                return
            end = h.pos + len(data)
            if end > len(h.data):
                h.data.extend(b"\x00" * (end - len(h.data)))
            h.data[h.pos:end] = data
            h.pos = end
            cpu.s.ax = len(data)
            cpu.set_flag(CF, False)
            return
        if ah == 0x44 and al == 0x00:  # IOCTL: get device information
            # Observed use (AEPROG.EXE): probing an opened .DAT handle to tell
            # file from character device.  DX bit 7 = 0 marks a block-device
            # file; low 6 bits are the drive (2 = C:, matching AH=19h above).
            # Std handles 0-2 answer as character devices (bit 7 set; stdin/
            # stdout/CON flag bits as a real DOS reports them).
            if cpu.s.bx in (0, 1, 2):
                cpu.s.dx = 0x80D3
                cpu.set_flag(CF, False)
                return
            if cpu.s.bx in self.files:
                cpu.s.dx = 0x0002
                cpu.set_flag(CF, False)
                return
            cpu.s.ax = 6  # invalid handle
            cpu.set_flag(CF, True)
            return
        if ah == 0x58:  # get/set allocation strategy
            # AL=00h get strategy, AL=01h set strategy.  current targets only need
            # this to succeed before DOS heap/free logic; keep first-fit.
            if al == 0x00:
                cpu.s.ax = 0x0000
                cpu.set_flag(CF, False)
                return
            if al == 0x01:
                cpu.set_flag(CF, False)
                return
            cpu.s.ax = 1
            cpu.set_flag(CF, True)
            return
        if ah == 0x47:  # get current directory
            # DS:SI receives an ASCIZ path without drive letter or leading slash.
            cpu.mem.wb(cpu.s.ds, cpu.s.si, 0)
            cpu.set_flag(CF, False)
            return
        if ah == 0x42:  # lseek
            h = self.files.get(cpu.s.bx)
            if h is None:
                cpu.s.ax = 6; cpu.set_flag(CF, True); return
            delta = ((cpu.s.cx << 16) | cpu.s.dx)
            if delta & 0x80000000:
                delta -= 0x100000000
            origin = al
            if origin == 0: h.pos = max(0, delta)
            elif origin == 1: h.pos = max(0, h.pos + delta)
            elif origin == 2: h.pos = max(0, len(h.data) + delta)
            cpu.s.dx = (h.pos >> 16) & 0xFFFF
            cpu.s.ax = h.pos & 0xFFFF
            cpu.set_flag(CF, False)
            return
        if ah == 0x48:  # allocate memory (BX paragraphs)
            paragraphs = cpu.s.bx & 0xFFFF
            if paragraphs == 0:
                cpu.s.ax = 8
                cpu.s.bx = self._largest_free_gap() & 0xFFFF
                cpu.set_flag(CF, True)
                return
            seg = self._find_free_gap(paragraphs)
            if seg is None:
                cpu.s.ax = 8  # insufficient memory
                cpu.s.bx = self._largest_free_gap() & 0xFFFF
                cpu.set_flag(CF, True)
                return
            self.allocations[seg] = paragraphs
            end = seg + paragraphs
            if end > self.next_alloc_segment:
                self.next_alloc_segment = end
            cpu.s.ax = seg
            cpu.set_flag(CF, False)
            return
        if ah == 0x49:  # free memory block (ES segment)
            # Reclaims the block: AH=48h/4Ah find free space by scanning gaps
            # between the CURRENT live allocations (see _find_free_gap), so
            # simply dropping the record is enough for the space to become
            # available again — no separate coalescing/free-list bookkeeping
            # needed. (Earlier this only dropped the record without ever
            # freeing the address range itself: a bump-pointer allocator with
            # no reuse, silently exhausting memory on any game that cycles
            # scratch buffers — SkyRoads does, 255 frees against 269 allocs
            # in one bring-up session; found 2026-07-09.)
            self.allocations.pop(cpu.s.es & 0xFFFF, None)
            cpu.set_flag(CF, False)
            return
        if ah == 0x4A:  # resize memory block (ES segment, BX paragraphs)
            seg = cpu.s.es & 0xFFFF
            new_size = cpu.s.bx & 0xFFFF
            old_size = self.allocations.get(seg)
            if old_size is None:
                cpu.s.ax = 7  # memory control blocks destroyed / unknown block
                cpu.set_flag(CF, True)
                return
            old_end = seg + old_size
            new_end = seg + new_size
            if new_end <= old_end:
                self.allocations[seg] = new_size
                cpu.set_flag(CF, False)
                return
            # Growing: fine as long as nothing else occupies [old_end, new_end).
            next_used = min((s for s in self.allocations if s >= old_end), default=self.allocation_limit_segment)
            if new_end > next_used or new_end > self.allocation_limit_segment:
                cpu.s.ax = 8
                cpu.s.bx = (min(next_used, self.allocation_limit_segment) - seg) & 0xFFFF
                cpu.set_flag(CF, True)
                return
            self.allocations[seg] = new_size
            if new_end > self.next_alloc_segment:
                self.next_alloc_segment = new_end
            cpu.set_flag(CF, False)
            return
        raise UnsupportedInstruction(f"Unhandled DOS INT 21h AH={ah:02X}h")

    def _free_gaps(self):
        """Yield (start, size_in_paragraphs) for every free gap between the
        current live allocations, in address order, up to allocation_limit_segment.
        First-fit, deterministic — matches how a real DOS MCB chain allocates
        by default, and (unlike a bump pointer) correctly reuses space a freed
        block gave back."""
        # Any freed block below the historical high-water mark is a reusable
        # gap too; start scanning from the lowest live allocation's floor
        # (normally the PSP block itself, which is always present).
        starts = sorted(self.allocations)
        cursor = min(starts, default=self.next_alloc_segment)
        for seg in starts:
            if seg < cursor:
                continue
            if seg > cursor:
                yield cursor, seg - cursor
            cursor = max(cursor, seg + self.allocations[seg])
        if cursor < self.allocation_limit_segment:
            yield cursor, self.allocation_limit_segment - cursor

    def _find_free_gap(self, paragraphs: int) -> int | None:
        for start, size in self._free_gaps():
            if size >= paragraphs:
                return start
        return None

    def _largest_free_gap(self) -> int:
        return max((size for _, size in self._free_gaps()), default=0)

    def int10(self, cpu: CPU8086) -> None:
        ah = (cpu.s.ax >> 8) & 0xFF
        al = cpu.s.ax & 0xFF
        if ah == 0x00:
            self.video_mode = al
            self.video_page = 0
            effective_mode = al & 0x7F
            self.text_mode_active = effective_mode in (0, 1, 2, 3, 7)
            if effective_mode in (0x13, 0x19):
                cpu.mem.ega_planar = False
            # A BIOS Set Video Mode always reloads the CRTC start address to 0.
            # We previously only did this for the linear modes (13h/19h); planar
            # mode 0Dh kept a stale display-start from the prior screen, which
            # shifted the level (the game relies on the BIOS reset and does not
            # re-write the start-address low byte for the play screen).
            cpu.mem.ega_display_start = 0
            # A mode-set also clears the attribute-controller pel-panning register, so a
            # stale fine-pan from a prior scrolling screen does not offset the new screen.
            cpu.mem.ega_pel_pan = 0
            cpu.mem.ega_pan_active = False
            cpu.mem.ega_pan_pel = 0
            cpu.mem.ega_h_display_end = 39   # BIOS graphics-mode default: 40 chars = 320px active
            cpu.mem.ega_display_enabled = True   # BIOS mode-set re-enables the display (PAS=1)
            self._attr_flipflop = False
            # Maintain the BIOS data area CRTC base port at 0040:0063 the way a
            # real BIOS mode-set does (color 3D4h / mono 3B4h).  Programs read it to
            # find the status port for retrace waits (e.g. via es=0, offset 0463h ==
            # flat 0040:0063).  (We deliberately do NOT touch 0040:0049/004A here —
            # the game manages its own video state and we keep this minimal.)
            crtc_base = 0x03B4 if effective_mode == 7 else 0x03D4
            cpu.mem.ww(0x0040, 0x0063, crtc_base)
            self.cursor_row = 0
            self.cursor_col = 0
            if self.text_mode_active:
                if not (al & 0x80):
                    self._clear_text_window(cpu, 0x07, 0, 0, 24, 79)
            else:
                self._clear_graphics_vram_for_mode(cpu, al)
            return
        if ah == 0x02:
            self.video_page = (cpu.s.bx >> 8) & 0xFF
            self.cursor_row = (cpu.s.dx >> 8) & 0xFF
            self.cursor_col = cpu.s.dx & 0xFF
            return
        if ah == 0x03:
            cpu.s.cx = 0x0607
            cpu.s.dx = ((self.cursor_row & 0xFF) << 8) | (self.cursor_col & 0xFF)
            return
        if ah == 0x05:
            # Select active display page.  Packed launchers
            # uses this during video setup before the inner game code starts.
            self.video_page = al
            return
        if ah in (0x06, 0x07):
            if al == 0:
                attr = (cpu.s.bx >> 8) & 0xFF
                top = (cpu.s.cx >> 8) & 0xFF
                left = cpu.s.cx & 0xFF
                bottom = (cpu.s.dx >> 8) & 0xFF
                right = cpu.s.dx & 0xFF
                self._clear_text_window(cpu, attr, top, left, bottom, right)
            return
        if ah == 0x0F:
            cpu.s.ax = (80 << 8) | self.video_mode
            cpu.s.bx = (cpu.s.bx & 0xFF00) | (self.video_page & 0xFF)
            return
        if ah == 0x12 and (cpu.s.bx & 0xFF) == 0x10:
            # EGA/VGA information query.  Launchers commonly set BL=10h and
            # treat BL unchanged after INT 10h as "no EGA/VGA"; report a colour
            # EGA/VGA with 256 KiB.
            cpu.s.bx = 0x0003
            cpu.s.cx = 0x0009
            return
        if ah == 0x1A and al == 0x00:
            # VGA read display combination code.  Games use this as the primary
            # VGA-presence probe: a VGA BIOS answers AL=1Ah (function supported)
            # with BL=active display (08h = colour analog VGA), BH=alternate
            # (00h = none).  Pre-VGA BIOSes leave AL unchanged.  First exercised
            # by AEPROG.EXE (Ancient Empires), which selects mode 13h on it.
            cpu.s.ax = (cpu.s.ax & 0xFF00) | 0x1A
            cpu.s.bx = 0x0008
            return
        if ah == 0x1B and al == 0x00:
            # VGA get functionality/state information.  PRE2.EXE calls this
            # during startup detection and passes ES:DI as a caller-owned buffer.
            # A real BIOS writes a 64-byte state table and returns AL=1Bh.  The
            # early game code only needs the call to be recognized as present,
            # so provide a conservative colour VGA/EGA-shaped table.
            table = bytearray(64)
            table[0] = 0x1B
            table[1] = 0x00
            table[2] = self.video_mode & 0x7F
            table[3] = 80
            table[4] = 25
            table[5] = self.video_page & 0xFF
            table[0x22] = 0x08  # colour display attached
            table[0x23] = 0x03  # 256 KiB display memory approximation
            for i, b in enumerate(table):
                cpu.mem.wb(cpu.s.es, (cpu.s.di + i) & 0xFFFF, b)
            cpu.s.ax = (cpu.s.ax & 0xFF00) | 0x1B
            return
        if ah == 0x0E:
            # BIOS teletype output.  Some games reach this from text input
            # name editor as a bell (AL=07h) on rejected input; keep it as a
            # narrow console side effect instead of trying to render BIOS text
            # over the game's graphics screen.
            self._write_text_char(cpu, al, (cpu.s.bx >> 8) & 0xFF or 0x07)
            if al != 0x07:
                self.stdout.append(chr(al))
            return
        if ah in (0x09, 0x0A):
            attr = (cpu.s.bx >> 8) & 0xFF if ah == 0x09 else None
            count = cpu.s.cx & 0xFFFF
            self._write_text_repeat(cpu, al, attr, count if count != 0 else 0x10000)
            return
        if ah == 0x11:
            # Load/select character-generator functions.  PRE2 installs a text
            # font during early setup.  The source-port VM does not need a BIOS
            # font ROM model yet; accepting the call is enough to keep startup
            # moving while the game's own graphics/font assets are decoded.
            return
        if ah == 0x10:
            # Palette / DAC control.  PRE2 loads custom palettes (the "oldies"
            # gold ramp, per-level palettes, ...) through these BIOS calls, so the
            # block/register sets must reach the DAC instead of being ignored.
            if len(self.vga_palette) < 256:
                self.__post_init__()
            if al == 0x12:  # set block of DAC registers from ES:DX (6-bit RGB triples)
                start = cpu.s.bx & 0xFF
                count = cpu.s.cx & 0xFFFF
                addr = cpu.s.dx & 0xFFFF
                for i in range(count):
                    r = cpu.mem.rb(cpu.s.es, addr)
                    g = cpu.mem.rb(cpu.s.es, (addr + 1) & 0xFFFF)
                    b = cpu.mem.rb(cpu.s.es, (addr + 2) & 0xFFFF)
                    self.vga_palette[(start + i) & 0xFF] = (_dac8(r), _dac8(g), _dac8(b))
                    addr = (addr + 3) & 0xFFFF
                return
            if al == 0x10:  # set one DAC register: BX=index, DH=R, CH=G, CL=B (6-bit)
                idx = cpu.s.bx & 0xFF
                self.vga_palette[idx] = (_dac8(cpu.s.dx >> 8), _dac8(cpu.s.cx >> 8), _dac8(cpu.s.cx))
                return
            if al == 0x17:  # read block of DAC registers into ES:DX
                start = cpu.s.bx & 0xFF
                count = cpu.s.cx & 0xFFFF
                addr = cpu.s.dx & 0xFFFF
                for i in range(count):
                    rgb = self.vga_palette[(start + i) & 0xFF]
                    cpu.mem.wb(cpu.s.es, addr, (rgb[0] >> 2) & 0x3F)
                    cpu.mem.wb(cpu.s.es, (addr + 1) & 0xFFFF, (rgb[1] >> 2) & 0x3F)
                    cpu.mem.wb(cpu.s.es, (addr + 2) & 0xFFFF, (rgb[2] >> 2) & 0x3F)
                    addr = (addr + 3) & 0xFFFF
                return
            # Attribute-palette sets (AL=00/02), blink toggle, etc.: accept as a
            # no-op (the attribute palette is identity for this game's screens).
            return
        if ah in (0x01, 0x0B, 0x12):
            return
        raise UnsupportedInstruction(f"Unhandled BIOS INT 10h AH={ah:02X}h")

    def int16(self, cpu: CPU8086) -> None:
        ah = (cpu.s.ax >> 8) & 0xFF
        if ah == 0x00:  # blocking read keystroke
            if self.key_queue:
                cpu.s.ax = self.key_queue.pop(0) & 0xFFFF
                return
            if self.console_input_fallback is None:
                cpu.s.ip = (cpu.s.ip - 2) & 0xFFFF
                raise ConsoleInputWouldBlock()
            cpu.s.ax = 0x011B  # Esc fallback keeps headless runs deterministic
            return
        if ah == 0x01:  # check keystroke: ZF=0 + AX=key if available, ZF=1 if not
            if self.key_queue:
                cpu.set_flag(ZF, False)
                cpu.s.ax = self.key_queue[0] & 0xFFFF
                return
            cpu.set_flag(ZF, True)
            return
        raise UnsupportedInstruction(f"Unhandled BIOS INT 16h AH={ah:02X}h")

    def note_bios_keystroke(self, scancode: int) -> None:
        """Update BIOS-visible keyboard state (modifiers + type-ahead buffer)
        for one raw scan code, as ``bios_int9_keyboard`` does -- factored out
        so a front-end injecting a key via ``deliver_scancode`` can update
        this state directly without faking a hardware-interrupt stack frame
        (see ``dos_re.interrupts.deliver_scancode``: on real hardware, one
        physical keypress updates BOTH a game's own key-state table AND the
        BIOS type-ahead buffer, since they observe the same INT 09h event;
        emulating only the former left the latter permanently empty, which
        breaks games that check it directly via INT 16h/INT 21h AH=0Bh --
        common at "press any key" prompts even in games whose main input
        loop uses a private key-state table for speed. Found via SkyRoads'
        post-level-select "press any key" screen never seeing input.
        """
        make = scancode & 0x7F
        released = bool(scancode & 0x80)
        if make == 0x2A or make == 0x36:      # left / right shift
            self.kbd_shift = not released
        elif make == 0x1D:                    # ctrl
            self.kbd_ctrl = not released
        elif make == 0x38:                    # alt
            self.kbd_alt = not released
        elif make == 0x3A and not released:   # caps lock (toggle on make)
            self.kbd_caps = not self.kbd_caps
        elif not released:
            value = self._bios_translate_scancode(make)
            if value is not None and len(self.key_queue) < 16:
                self.key_queue.append(value & 0xFFFF)

    def bios_int9_keyboard(self, cpu: CPU8086) -> None:
        """The IBM-PC BIOS INT 09h keyboard ISR (IRQ1).

        This is the power-on keyboard handler.  A DOS game commonly installs its
        own INT 9 ISR to maintain a live key-state table and then *chains* to the
        previous (this) handler so ordinary keystrokes still reach the BIOS
        type-ahead buffer that INT 16h reads.  Ancient Empires does exactly that:
        its menus poll INT 16h for navigation (arrows arrive as extended codes,
        AL=0/AH=scancode) while gameplay reads its own table -- the game decides
        per key whether to chain, so this handler needs no game knowledge.

        It is installed at a dedicated BIOS entry (runtime.BIOS_INT9_ENTRY) that
        the power-on IVT[9] points to, so the game saves and chains to it.  Entry
        frame is a hardware-interrupt frame (flags, cs, ip on the stack); it acks
        the PIC and returns with IRET.
        """
        self.note_bios_keystroke(self.current_scancode & 0xFF)

        if cpu.port_writer:                   # EOI to the master PIC
            cpu.port_writer(cpu, 0x20, 0x20, 8)
        # IRET: pop ip, cs, flags (entered via hardware-interrupt frame).
        cpu.s.ip = cpu.pop()
        cpu.s.cs = cpu.pop()
        cpu.s.flags = cpu.pop() | 0x0002

    def _bios_translate_scancode(self, make: int) -> int | None:
        """Return the 16-bit BIOS buffer word (AH=scancode, AL=ASCII) for a make
        code, or None for keys the BIOS does not buffer (pure modifiers, etc.)."""
        if make in _BIOS_EXTENDED_KEYS:       # arrows, F-keys, nav cluster: AL=0
            return (make << 8)
        entry = _BIOS_SCANCODE_ASCII.get(make)
        if entry is None:
            return None
        base, shifted = entry
        if base.isalpha():
            upper = self.kbd_shift ^ self.kbd_caps
            ch = shifted if upper else base
        else:
            ch = shifted if self.kbd_shift else base
        return (make << 8) | (ord(ch) & 0xFF)

    @staticmethod
    def _bcd(value: int) -> int:
        value = max(0, min(99, value))
        return ((value // 10) << 4) | (value % 10)

    def int1a(self, cpu: CPU8086) -> None:
        ah = (cpu.s.ax >> 8) & 0xFF
        if ah == 0x00:
            self.ticks += 1
            cpu.s.cx = (self.ticks >> 16) & 0xFFFF
            cpu.s.dx = self.ticks & 0xFFFF
            cpu.s.ax &= 0xFF00
            return
        if ah == 0x01:  # Set system time-of-day counter (CX:DX -> tick count).
            # Games write the counter back to (re)base their timing — VGA
            # Lemmings sets it when leaving a level.  Subsequent AH=00h reads
            # continue from the written value (same self.ticks the getter
            # serves), and a real BIOS also clears the midnight-rollover flag
            # (AL on the next read; this model never sets it anyway).
            self.ticks = ((cpu.s.cx & 0xFFFF) << 16) | (cpu.s.dx & 0xFFFF)
            return
        if ah == 0x02:  # Get real-time clock time (BCD CH=h CL=m DH=s DL=daylight flag).
            now = datetime.now()
            cpu.s.cx = (self._bcd(now.hour) << 8) | self._bcd(now.minute)
            cpu.s.dx = (self._bcd(now.second) << 8)
            cpu.set_flag(CF, False)
            return
        if ah == 0x04:  # Get real-time clock date (BCD CH=century CL=year DH=month DL=day).
            today = date.today()
            cpu.s.cx = (self._bcd(today.year // 100) << 8) | self._bcd(today.year % 100)
            cpu.s.dx = (self._bcd(today.month) << 8) | self._bcd(today.day)
            cpu.set_flag(CF, False)
            return
        raise UnsupportedInstruction(f"Unhandled BIOS INT 1Ah AH={ah:02X}h")

    def int2f(self, cpu: CPU8086) -> None:
        ax = cpu.s.ax & 0xFFFF
        ah = (ax >> 8) & 0xFF
        if ah == 0x43:
            # XMS multiplex API.  PRE2 probes for an XMS driver with AX=4300h.
            # Report "not installed" so the original fallback path stays in
            # conventional memory; do not fake an XMS control entry point yet.
            cpu.s.ax = ax & 0xFF00
            return
        # INT 2Fh is a multiplex interrupt: unsupported installation checks are
        # commonly treated as "service absent" rather than fatal during DOS-game
        # bring-up.  Leave registers unchanged for unknown multiplex IDs.
        return

    def int15(self, cpu: CPU8086) -> None:
        """BIOS system services.  On the PC/XT-class 8086 machine this VM
        models, extended services are absent: the faithful response is CF set
        with AH=86h ("function not supported"), which machine-type detection
        code reads as "old PC".  Observed use: AH=C0h (get system
        configuration) during AEPROG.EXE hardware detection."""
        ah = (cpu.s.ax >> 8) & 0xFF
        if ah == 0xC0:
            cpu.s.ax = 0x8600 | (cpu.s.ax & 0x00FF)
            cpu.set_flag(CF, True)
            return
        raise UnsupportedInstruction(f"Unhandled BIOS INT 15h AH={ah:02X}h")

    def set_mouse_norm(self, u: float, v: float, buttons: int | None = None) -> None:
        """Update the mouse from window-relative coordinates (0.0..1.0), mapped
        onto the program's own virtual range (AX=7/8) so the pointer is
        proportional whatever coordinate box the game chose.  A front-end (or a
        probe) calls this; left untouched, the mouse stays at rest."""
        r = self.mouse_range
        u = 0.0 if u < 0 else (1.0 if u > 1 else u)
        v = 0.0 if v < 0 else (1.0 if v > 1 else v)
        self.mouse_x = r[0] + int(u * (r[1] - r[0]))
        self.mouse_y = r[2] + int(v * (r[3] - r[2]))
        if buttons is not None:
            self.mouse_buttons = buttons

    def int33(self, cpu: CPU8086) -> None:
        # Minimal Microsoft mouse driver.  State is fed by the front-end via
        # set_mouse_norm; services are grown as games issue them.  Reporting the
        # mouse PRESENT (AX=0 -> AX=FFFF) is what makes a mouse-driven game (VGA
        # Lemmings) enable pointer control at all — the previous stub reported it
        # absent, so the game gave up after its one reset/detect call.
        ax = cpu.s.ax & 0xFFFF
        if ax == 0x0000:                         # reset/detect -> AX=FFFF, BX=#buttons
            cpu.s.ax = 0xFFFF
            cpu.s.bx = 2
            self.mouse_x, self.mouse_y, self.mouse_buttons = 160, 100, 0
            self.mouse_range = [0, 639, 0, 199]  # driver reset restores full ranges
            return
        if ax in (0x0001, 0x0002):               # show / hide cursor (game draws its own)
            return
        if ax == 0x0003:                         # get position + buttons (range-clamped)
            r = self.mouse_range
            cpu.s.bx = self.mouse_buttons & 0xFFFF
            cpu.s.cx = max(r[0], min(r[1], self.mouse_x)) & 0xFFFF
            cpu.s.dx = max(r[2], min(r[3], self.mouse_y)) & 0xFFFF
            return
        if ax == 0x0004:                         # set position (CX, DX)
            self.mouse_x = cpu.s.cx & 0xFFFF
            self.mouse_y = cpu.s.dx & 0xFFFF
            return
        if ax == 0x0007:                         # set horizontal range (CX..DX)
            self.mouse_range[0], self.mouse_range[1] = cpu.s.cx & 0xFFFF, cpu.s.dx & 0xFFFF
            return
        if ax == 0x0008:                         # set vertical range (CX..DX)
            self.mouse_range[2], self.mouse_range[3] = cpu.s.cx & 0xFFFF, cpu.s.dx & 0xFFFF
            return
        if ax == 0x000B:                         # read motion counters -> CX/DX deltas
            cpu.s.cx = 0
            cpu.s.dx = 0
            return
        # Unimplemented subfunction: no-op (grow when a game proves it needs it).
        return

    def int67(self, cpu: CPU8086) -> None:
        """Minimal EMS interrupt model: report EMS driver/functions unavailable.

        PRE2 probes expanded memory while transitioning from the title/options
        screens into level loading.  A real DOS machine without EMM386/QEMM has
        no usable EMS driver, and well-behaved games then fall back to
        conventional memory.  The EMS API returns status in AH rather than using
        DOS carry semantics; 80h is the generic "function unsupported / driver
        unavailable" error used by EMS clients to abandon the path.
        """
        ah = (cpu.s.ax >> 8) & 0xFF
        if 0x40 <= ah <= 0x5F:
            cpu.s.ax = 0x8000 | (cpu.s.ax & 0x00FF)
            return
        cpu.s.ax = 0x8000 | (cpu.s.ax & 0x00FF)
        return
