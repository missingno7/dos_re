# Hardware model support

dos_re models the hardware behavior needed by recovered programs; it is not a
complete PC emulator. This document describes framework capabilities, not a
claim that a particular program has exercised every path. A port must retain
its own replay, test, and runtime evidence for the devices it selects.

## Status terms

- **covered**: implemented with focused framework tests for the stated subset;
- **partial**: a deliberately limited subset; behavior outside it may fail
  loud or require extension;
- **adapter-supplied**: the machine state exists, but presentation or host I/O
  belongs to the port adapter;
- **detection-only**: detection state is modeled without producing device
  output;
- **fail-loud**: unsupported behavior raises with diagnostic context;
- **absent**: no model is provided.

“Covered” never means datasheet-complete. Extend a device model only from
cited program observations and keep game-specific policy in the port.

## Unmodeled port I/O

By default, an unknown port read returns zero and is recorded in
`dos.unmodeled_port_reads`; an unknown write is retained in `dos.port_log` and
otherwise ignored. This benign default supports common detection probes but can
hide a missing device when program logic consumes the value.

Set:

```python
runtime.dos.strict_ports = True
```

to raise `UnmodeledPortRead` with the reading execution point. Audit strict-port
results when introducing a program or changing its selected implementations.
An empty observation log is evidence only for the exercised intervals, not
proof that an unmodeled port is unreachable.

## Real-mode video

| Capability | Status | Owner |
|---|---|---|
| VGA mode 13h linear framebuffer | covered | `dos.py`, `memory.py` |
| VGA DAC ports 3C7/3C8/3C9 and pixel-mask probe | covered | `dos.py` |
| VGA/EGA planar aperture, map/read plane, latches, write modes 0–1, read modes 0–1, rotate/logical operations | partial; unsupported write modes fail loud | `memory.py`, `dos.py` |
| CRTC display start, attribute pel panning, horizontal display-end narrowing | covered subset | `dos.py`, `memory.py` |
| Vertical retrace status | covered with deterministic or explicit wall-clock source | `dos.py` |
| BIOS text modes and teletype | partial | `dos.py` |
| CGA B800 memory and mode/palette state | adapter-supplied rasterization | `dos.py`, `memory.py` |
| Tandy/PCjr memory and mode state | adapter-supplied rasterization | `dos.py`, `memory.py` |
| RGB sampling and presentation | adapter-supplied | project backend |

## Audio and interrupt devices

| Capability | Status | Owner |
|---|---|---|
| AdLib/OPL register file and timer-status detection | covered subset | `dos.py` |
| OPL2/OPL3 synthesis | approximate bundled backend; optional external bit-exact backend | `opl3_fast.py`, `audio_sink.py` |
| PC speaker gate and PIT channel 2 frequency | covered subset | `dos.py` |
| Sound Blaster DSP, DMA, block IRQs, and detection-only mode | partial, opt-in | `sblaster.py`, `runtime_core.py` |
| 8259 PIC acknowledgement, EOI, priority, and mask | covered subset | `pic.py` |
| Roland/MPU-401, GUS, Covox | absent | — |
| Game sequencers and software mixers | program implementation, not hardware model | selected implementation |

The bundled OPL synthesizer is approximate. Set
`DOSRE_OPL3_BACKEND=nuked` only when the optional external
`pynuked_opl3` package is installed and bit-exact output is required.

## Timing, keyboard, and interrupts

| Capability | Status | Owner |
|---|---|---|
| PIT channel 0 reload, programmed rate, latch/read | covered subset | `dos.py` |
| PIT channel 2 speaker timing | covered subset | `dos.py` |
| INT 08h delivery through deterministic, frontend-paced, or PIC-driven clocks | covered modes | `interrupts.py`, `cpu.py`, `pic.py` |
| INT 09h scancodes and 8042 output-buffer status | covered subset | `interrupts.py`, `keyboard.py`, `dos.py` |
| Wall-clock pacing and retrace timing | opt-in runtime service | `cpu.py`, `dos.py` |

Deterministic replay and verification should use deterministic time sources.
Wall-clock services must be declared by the execution plan and are not
interchangeable continuation state.

## Protected mode

| Capability | Status | Owner |
|---|---|---|
| MZ+LE loading, internal fixups, and rebasing above 1 MiB | covered subset | `le.py` |
| Flat 386 registers, ModR/M+SIB, selector bases, IRQ delivery, and x87 subset | partial; unsupported opcodes fail loud | `cpu386.py` |
| Observed DOS/4GW, DOS, BIOS, DPMI, and extender services | partial; unknown services fail loud | `dos4gw.py` |
| 8042 command/ACK protocol and per-byte IRQ1 scancodes | covered subset | `dos4gw.py` |
| VGA mode 13h and Mode X aperture, DAC, display start, and retrace | partial; unsupported write modes and masks fail loud | `dos4gw.py`, `cpu386.py` |
| Instruction-count-driven timer IRQ0 | covered, opt-in | `dos4gw.py` |
| Sound Blaster detection, DMA, block pacing, and PIC-gated IRQs | partial, opt-in | `dos4gw.py`, `sblaster.py`, `pic.py` |
| Real-mode callbacks and mode-switch DPMI services | absent | — |

## DOS, BIOS, and CPU scope

The real-mode runtime provides the observed subset of DOS file and memory
services, console services, PSP and command-tail state, BIOS startup data,
minimal allocation, and clean absence probes for unsupported memory managers.
File handles retain deterministic offsets. These behaviors are owned by
`dos.py`, `memory.py`, and `runtime.py`.

The 16-bit CPU covers the 8086/8088 core plus observed 80186 instructions and
selected 386-probe prefix behavior. Unsupported opcodes fail loud. Exact flag,
wrap, repeat, rotate, and shift behavior is implemented only for covered
operations; a newly observed case requires a focused CPU test and oracle
evidence.

## Extension rule

For any new hardware behavior:

1. capture the execution point, inputs, outputs, and timing or interrupt
   contract observed from the original program;
2. add the smallest game-agnostic model that explains that evidence;
3. add focused tests, including unsupported adjacent behavior;
4. replay the affected oracle/candidate interval and compare complete
   continuation state;
5. update this status table without generalizing beyond the evidence.

Hardware behavior belongs in dos_re. A program's device selection, assets,
presentation, and recovered driver logic belong in its implementation catalog
and adapters.
