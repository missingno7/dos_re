# Hardware model support — honest status

What the VM actually models today, where it came from, and what a new game may
have to add. "Modeled" means: exercised end-to-end by at least one of the two
source ports (Overkill on CGA/EGA/Tandy hardware paths, Prehistorik 2 on
VGA/SB), with behaviour derived from what those games' oracles demanded — not
from datasheet completeness. Expect to extend models for what *your* game
exercises; add only what its oracle proves it needs.

## Video

| Area | Status | Where |
|---|---|---|
| VGA mode 13h (linear 320×200×256) | modeled | `dos.py` INT 10h + A000h in `memory.py` |
| VGA DAC (3C7/3C8/3C9, 6-bit→8-bit, pixel mask probe) | modeled | `dos.py` |
| VGA/EGA planar modes (4 planes behind A000h, map mask, read plane, write modes 0–1, read modes 0–1 incl. color-compare, latches, data-rotate/logical-op) | modeled — caveat: write modes 2–3 silently behave as mode 0 (known gap, see MIGRATION.md) | `memory.py` (EGA aperture) + `dos.py` (sequencer/GC ports) |
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
| OPL2/OPL3 FM synthesis (actual sound) | vendored | `nuked_opl3/` (cffi binding to Nuked-OPL3; build with `pip install cffi` + `python -m nuked_opl3._ffi_build`) |
| PC speaker (port 61h gate + PIT channel 2 frequency) | modeled | `dos.py` (`speaker_callback`) |
| Sound Blaster DSP + DMA + block IRQs (+ detection-only stub mode) | modeled | `sblaster.py`, wired by `runtime.enable_sound_blaster` |
| 8259 PIC (IRQ raise/acknowledge/EOI, priority, mask) | modeled | `pic.py` |
| Roland/MPU-401, GUS, Covox | **not modeled** | — |
| Game-specific sound *drivers* (sequencers, mixers) | game code by definition — recover them per game. Overkill's AdLib driver bootstrap and PC-speaker engine, and pre2's tracker/mixer, are worked examples in the source repos. | adapter work |

## Timing / interrupts

| Area | Status | Where |
|---|---|---|
| PIT channel 0 (reload tracking, programmed Hz property) | modeled | `dos.py` |
| PIT channel 2 (speaker) | modeled | `dos.py` |
| INT 08h delivery (deterministic, front-end-paced, or PIC-driven inline via `cpu.pending_irq`) | modeled | `interrupts.py`, `cpu.py`, `pic.py` |
| INT 09h keyboard (port 60h scancode + game ISR) | modeled | `interrupts.py`, `keyboard.py` |
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
