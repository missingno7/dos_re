# Super Solvers: Challenge of the Ancient Empires — RE knowledge base

Consolidated, **verified-only** reverse-engineering knowledge for
*Super Solvers: Challenge of the Ancient Empires* (The Learning Company, DOS),
prepared as input for an oracle-driven port built on `dos_re`.

## Provenance

Everything here was distilled from
`D:\Games\DOS\ancient-empires-reverse-engineered` (the existing Python source
port + research editor). Only facts that meet at least one of these bars were
kept:

- traced in the full disassembly of `AEPROG.EXE` (the project's
  `AEPROG_full_disasm.asm`), with the routine address recorded;
- implemented in the working Python port and exercised by its test suite
  (`tests/test_player_movement.py`, `test_laser.py`, `test_audio_timing.py`,
  `test_room_transitions.py`, …);
- verified against captures from the real game running in DOSBox
  (audio DRO captures, screenshots, recordings).

Facts the source project itself marks as partial/observed-but-not-EXE-derived
are either omitted or explicitly flagged **UNPROVEN** in each doc. Knowing what
is *not* proven is as important for the oracle loop as what is.

## Game identity

| Item | Value |
|---|---|
| Executable | `AEPROG.EXE` — plain DOS MZ, **not** packed (no LZEXE) |
| MZ header size | `0x200` bytes |
| DGROUP | paragraph `0x0FA3` → loaded-image offset `0x0FA30` |
| Data files | `AE000.DAT`, `AE001.DAT` (resource archives, same format) |
| Video | EGA planar rendering (`out` runs to `0x3CE`/`0x3C4`); VGA gets a custom 256-colour DAC palette via INT 10h `AX=1012h`; EGA palette registers remapped via INT 10h `AX=1000h`; CGA supported via per-sprite colour tables |
| Audio | PC speaker (PIT ch.2, ports `0x42`/`0x61`), Tandy/PCjr SN76489 (port `0xC0`), AdLib/OPL2 (YM3812). **No PCM/samples, no Sound Blaster DSP, no DMA** |
| Master timer | timer ISR at `0x6BCF`, PIT reload `0x13B1` (5041) → **~236.69 Hz** master tick |
| Levels | 20 caverns × 2 difficulties (Explorer/Expert), 10 rooms each |

## Address conventions used in these docs

- `0xNNNN` code addresses are offsets as used in the project's flat
  disassembly of the loaded `AEPROG.EXE` image.
- `DS:xxxx` are data-segment (DGROUP) offsets.
  File offset of a DS variable = `0x200 + 0xFA30 + xxxx`
  (e.g. the OPL patch table `DS:301A` is at file offset `0x12C4A`).

## Documents

| Doc | Contents |
|---|---|
| [`dat_archives.md`](dat_archives.md) | AE00x.DAT archive layout, RLE + LZW-like decompression (exact algorithms), resource types |
| [`graphics.md`](graphics.md) | Type `0x47` bitmap format, VGA/EGA/CGA palette pipelines, bitmap fonts, monochrome symbol bank |
| [`levels.md`](levels.md) | Level resource format: part header, rooms, terrain, payload directory, controls, puzzles, platforms, render order, coordinate anchors |
| [`actors.md`](actors.md) | Actor table record layout, shared script space, complete researched actor-VM opcode set, timing |
| [`audio.md`](audio.md) | Type `0x44` audio containers, PC-speaker SFX engine (CAF1), music bytecode, OPL patch pipeline, SFX id catalogue |
| [`gameplay.md`](gameplay.md) | Player movement/collision, room transitions, tools & laser physics, control activation, puzzles, HUD, menus/map |
| [`exe_map.md`](exe_map.md) | The hook goldmine: routine-address tables and DS variable map for `dos_re` hooks and state mirrors |

## Suggested dos_re bring-up notes

- The EXE is unpacked; `create_runtime` should boot it directly — no
  `bootstrap_lzexe` step needed.
- This is a **mixed-style** game leaning data-driven: level/actor behaviour is
  bytecode interpreted by a small actor VM (see `actors.md`) — round-trip
  decode tests carry proof there — while player physics, the laser and puzzles
  are hardcoded routines (see `gameplay.md`, `exe_map.md`).
- Frame boundary candidates: timer ISR `0x6BCF`; the player loop reschedules
  itself every `0x18` (24) master ticks at `0x3AA5`; actor VM advances every 24
  master ticks. One gameplay tick = 24 master ticks (~9.862 Hz) is the natural
  verification cadence.
- Keyboard is a custom INT 09h ISR around `0x69F5` (own key-state words, not
  BIOS) — deliver scancodes, then check `DS:0B68`-family state.
- Known-good state-mirror seeds: player state block `DS:072C..0740`, current
  room `DS:BFBA`, level part copy at `DS:4374`, actor table at `DS:B3AE`,
  control records at `DS:BFC0` (see `exe_map.md`).
