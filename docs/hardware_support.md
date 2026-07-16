# Hardware model support — honest status

What the VM actually models today, where it came from, and what a new game may
have to add. Expect to extend models for what *your* game exercises; add only
what its oracle proves it needs.

**Status legend** (a claim is only as strong as its oracle):

- **modeled** — exercised end-to-end by at least one source port's oracle
  (Overkill on CGA/EGA/Tandy paths, Prehistorik 2 on VGA/SB); behaviour derived
  from what those games demanded, not from datasheet completeness.
- **minimal** — enough for the observed uses; thin beyond them.
- **detection-only** — the program detects the device and its commands are
  captured, but no output is produced (e.g. the Sound Blaster stub mode).
- **VM-level only** — programs run and write the memory/ports, but there is no
  rasterizer/present model; that part is adapter work.
- **fails loud** — explicitly unimplemented; raises with context when hit.
- **not modeled** — absent entirely; the interpreter's behaviour when touched
  is documented below under "unmodeled I/O policy".

**Unmodeled I/O policy:** reads from ports the model does not know return 0
and writes are logged but otherwise ignored — this is the *proven* behaviour
both source games ran under (their detection probes rely on benign defaults),
so it is kept as the default. It is also a documented soft spot: a program
whose *logic* consumes an unmodeled port read gets a silently wrong 0. Two
mitigations ship: every unmodeled read is recorded in
`dos.unmodeled_port_reads` (capped; audit it early whenever a new game's
behaviour looks impossible, alongside `dos.port_log`), and setting
`rt.dos.strict_ports = True` makes such reads **fail loud**
(`UnmodeledPortRead`, with the reading CS:IP) for recovery/audit sessions.

## Protected mode (DOS/4GW / LE titles) — the flat-386 path

| Area | Status | Where |
|---|---|---|
| LE (MZ+`LE`) loading, internal fixups, rebase above 1 MB | modeled | `le.py` |
| Flat 386 CPU (32-bit regs, ModRM+SIB, x87 subset, selector bases, IRQ delivery via installed IDT) | modeled — unimplemented opcodes **fail loud** | `cpu386.py` |
| DOS/4GW services (INT 21h/2Fh/10h/31h/33h subset, DPMI DOS-memory alloc, extender probes FF00/ED/1687) | modeled from observed calls — unknown services **fail loud** | `dos4gw.py` |
| 8042 keyboard controller (command/ACK protocol, per-byte IRQ1 scancodes) | modeled | `dos4gw.py` |
| VGA on the PM path: mode 13h linear, Mode X planar (map mask, read map, write modes 0–1 w/ latches), DAC, CRTC display start, 3DAh retrace (deterministic toggle or wall clock) | modeled — write modes 2–3 and non-FF bit mask **fail loud** | `dos4gw.py` (`VGASequencer`), `cpu386.py` (aperture routing) |
| PM timer IRQ0 (instruction-count-driven, deterministic, off by default) | modeled | `dos4gw.py` |
| Sound Blaster on the PM path (DSP detection, DMA-channel autodetect via real memory effects + 8237 readback/TC status, block-paced PCM via PIC-gated IRQs, deterministic emulated block clock) | modeled — `attach_sound_blaster`; the viewer's `_PcmSink` plays the stream | `dos4gw.py`, `sblaster.py`, `pic.py` |
| 8259 PIC on the PM path (mask, in-service EOI gating) | modeled | `dos4gw.py` + `pic.py` |
| Real-mode callbacks / mode-switch DPMI services | **not modeled** — no target exercised them | — |

## Video

| Area | Status | Where |
|---|---|---|
| VGA mode 13h (linear 320×200×256) | modeled | `dos.py` INT 10h + A000h in `memory.py` |
| VGA DAC (3C7/3C8/3C9, 6-bit→8-bit, pixel mask probe) | modeled | `dos.py` |
| VGA/EGA planar modes (4 planes behind A000h, map mask, read plane, write modes 0–1, read modes 0–1 incl. color-compare, latches, data-rotate/logical-op) | modeled — write modes 2–3 **fail loud** (`UnsupportedEgaWriteMode`); implement from your oracle when a game hits them | `memory.py` (EGA aperture) + `dos.py` (sequencer/GC ports) |
| CRTC display start + attribute pel-panning (smooth scroll), horizontal display-end narrowing | modeled | `dos.py`, `memory.py` |
| Vertical retrace status (3DA), deterministic or wall-clock-driven, tunable active fraction | modeled | `dos.py` |
| BIOS text modes / teletype output | minimal but present | `dos.py` INT 10h |
| CGA (B800 memory, mode 4/5 palettes) | **VM-level only** — programs run and write B800; there is **no generic CGA rasterizer/present model**. Overkill's CGA present path was game-specific lifted code and was not extracted. | adapter work |
| Tandy/PCjr video | **VM-level only** — same status as CGA. Overkill's Tandy mode-2 renderer (`overkill/rendering/tandy.py`) is game-specific and stayed behind. | adapter work |
| Rasterization to RGB for viewing/diffing | adapter-supplied (`sample_builder`); the pre2 port's VGA/EGA rasterizers are per-game code | adapter work |

## Audio

| Area | Status | Where |
|---|---|---|
| AdLib/OPL2 register file + timer-status detection handshake (ports 388/389) | modeled | `dos.py` (`opl_registers`, `adlib_callback`) |
| OPL2/OPL3 FM synthesis (actual sound) | modeled | `dos_re/opl3_fast.py` — numpy approximate synth, perceptually matched to the exact chip (the default and only bundled backend); the external `pynuked_opl3` package is an opt-in bit-exact upgrade (`DOSRE_OPL3_BACKEND=nuked`). Bit-exact core dormant in `graveyard/opl3_exact.py`. Wired to viewers via `dos_re.audio_sink`. |
| PC speaker (port 61h gate + PIT channel 2 frequency) | modeled | `dos.py` (`speaker_callback`) |
| Sound Blaster DSP + DMA + block IRQs (+ detection-only stub mode) | modeled | `sblaster.py`, wired by `runtime.enable_sound_blaster` |
| 8259 PIC (IRQ raise/acknowledge/EOI, priority, mask) | modeled | `pic.py` |
| Roland/MPU-401, GUS, Covox | **not modeled** | — |
| Game-specific sound *drivers* (sequencers, mixers) | game code by definition — recover them per game. Overkill's AdLib driver bootstrap and PC-speaker engine, and pre2's tracker/mixer, are worked examples in the source repos. | adapter work |

## Timing / interrupts

| Area | Status | Where |
|---|---|---|
| PIT channel 0 (reload tracking, programmed Hz property, direct reads via the latch command + port 40h) | modeled | `dos.py` |
| PIT channel 2 (speaker) | modeled | `dos.py` |
| INT 08h delivery (deterministic, front-end-paced, or PIC-driven inline via `cpu.pending_irq`) | modeled | `interrupts.py`, `cpu.py`, `pic.py` |
| INT 09h keyboard (port 60h scancode + game ISR) + 8042 controller status (port 64h output-buffer-full bit) | modeled | `interrupts.py`, `keyboard.py`, `dos.py` |
| Wall-clock pacing (`timer_pacer`, retrace `time_source`) | modeled, opt-in — deterministic paths leave it off | `cpu.py`, `dos.py` |

## DOS / BIOS services

INT 21h (files, memory allocation, console, exec-adjacent state), PSP + command
tail, minimal allocator, XMS/EMS *absence* probes (programs detect "no driver"
cleanly), BIOS power-on environment (IVT IRET stubs, BIOS data area CRTC base).
File I/O keeps exact handle offsets. All in `dos.py` / `memory.py` /
`runtime.py`.

## CPU

8086/8088 + the 80186 instructions (PUSH imm, shift imm, …) and the 386-probe
paths (operand-size prefix behaviour) that shipped games actually use.
`LOOP`/`REP` wrap semantics, rotate/shift flag shapes, undefined-flag behaviours
observed by real games are matched where a source port exercised them. When
your game hits an unimplemented opcode, the interpreter fails loud — implement
exactly the observed behaviour and add a `tests/test_core.py`-style case.

## The rule for extending any of this

The original executable is the oracle. Model the *hardware behaviour the
program observes* — no more. Document the call site and observed register
contract for every new port/service behaviour, and keep it game-agnostic (the
game's *use* of the hardware is adapter knowledge; the hardware's *behaviour*
is framework knowledge).
