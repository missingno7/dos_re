# Level resource format

`AE001.DAT` resources `0..19` are the 20 levels. Status: layout **CANONICAL**;
individual payload-object semantics as flagged.

## Top level

A decoded level resource starts with magic byte `0x4D` and splits into two
equal difficulty parts: **part 0 = Explorer, part 1 = Expert**.

Each part:

```text
0x0000..0x003F  part header (0x40 bytes)
0x0040..0x274F  10 room records × 1000 bytes
0x2750..0x2753  4-byte separator
0x2754..0x330B  actor block, 0x0BB8 (3000) bytes   (see actors.md)
```

At runtime AEPROG copies the first `0x2750` bytes of the active part to
`DS:4374` and the actor block to `DS:B3AE`.

There are exactly **10 real rooms**. (Early tooling that showed rooms 10..12
was mis-parsing the actor block; those are not rooms.)

## Part header (known fields)

```text
header[0x02] & 0x03    theme index (terrain bank 21+theme, decor bank 25+theme)
header[0x03]           player start x        (start room is room 0, hardcoded)
header[0x04]           player start y
header[0x05]           conditional exit door: room index (zero-based)
header[0x06]           exit door x
header[0x07]           exit door y
header[0x08..0x0D]     six artifact-pickup room ids (room-gated pickups)
header[0x0E..0x13]     six pickup x values
header[0x14..0x19]     six pickup y values
header[0x1A..0x23]     left  room links, 10 bytes (indexed by room)
header[0x24..0x2D]     right room links
header[0x2E..0x37]     up    room links
header[0x38..0x41]     down  room links
```

Room links are **one-based** room ids; `0` = no link. (The runtime copies of
the four link arrays live at `0x438C/0x4396/0x43A0/0x43AA`.)

The conditional exit door appears after all artifacts are collected; its
artwork is **sprite 0 of the current theme terrain bank** (`AE001:021..024`).

## Room record (1000 bytes)

```text
+0x000..0x001  preamble / room metadata (semantics UNPROVEN)
+0x002..0x2AD  terrain grid: 38 columns × 18 rows, row-major, 1 byte/cell
+0x2AE..0x3E7  trailing payload, 314 bytes
```

Viewport: 38×18 cells × 8 px = **304×144 px**.

### Terrain byte semantics (proven subset)

- Low three bits (`tile & 0x07`) model passability: `0` passable, non-zero
  solid. `0x07` is an **invisible solid support/collision tile** (used under
  platforms and green blocks; it moves with them at runtime).
- `0x0F` / `0x1F`: conveyor physics tile runs (grey / teal direction). The
  *visible* belt art comes from CV payload records — conveyors are composite.
- `0x80..0xC0`: rope-family markers (zero low bits → passable/climbable).
- The complete tile-code → sprite lookup table has **not** been recovered from
  the EXE; the port uses a confirmed-by-observation partial mapping.
  **UNPROVEN: full terrain lookup table.**

## Trailing room payload (offsets relative to +0x2AE)

### Moving platforms — first 30 bytes

Ten 3-byte platform triplets at `+0x00..0x1D`: `flags, x_raw, y`.

Flag families (proven art/collision behaviour): `0x40/0x60` horizontal,
`0x80/0xA0` vertical. The static draw (`0x28AC`) also writes the platform's
`0x07` collision footprint into terrain.

**UNPROVEN**: the exact motion table. Observed travel used by the port:
`0x40 → +48 px x`, `0x60 → −48 px x`, `0x80 → +48 px y`, `0xA0 → −48 px y`.
The triplet does not store an explicit destination.

### Runtime apple slot

`room[0x3E5..0x3E7]` is the room-gated apple/pickup runtime slot (cleared on
pickup at `0x3C4A`; drawn at `0x2E89`).

### Variable payload directory — starts at `+0x1E`

```text
+0x1E  directory count / selector family
...    length-prefixed control records
...    section_a compact3 table  — wall-symbol buttons/emitters (puzzle markers)
...    section_b record12 table  — green-block mechanisms
...    section_c compact3 table  — laser crystals / reflectors
...    main visual compact3 table — theme/global decor
...    4-byte animated-decor records (front of directory; handlers 0xD81C/0xD99C)
...    12-byte animated table after the visuals (refreshed by 0xD586)
```

### Control records (length-prefixed)

```text
raw[0]   length
raw[1]   command:  0x00 ceiling button, 0x01 floor switch,
                   0x02 laser trigger / light sensor / lever family
raw[2..] arguments
```

- `arg_b & 0x40` = pressed/start-state bit (selects pressed artwork,
  `0x9B6E` vs `0x9AE0`).
- Target bytes: `0x00..0x0F → P0..P15` (platform slots),
  `0x10..0x1F → CV0..CV15` (conveyor records),
  `0x40..0x4F → R0..R15` (section_c reflectors).
- One control can list multiple targets. Multiple **active** controls aimed at
  the same target combine by **parity/XOR** (two active sources cancel).
- Runtime records live at `DS:BFC0` (`+1` command, `+2` x, `+3` y,
  `+4` pressed). `0x32FA` toggles a control's terrain effect (XOR bit `0x10`);
  `0x338A` activates one (flips `+4`, plays a switch SFX).
- **UNPROVEN**: full semantics of every control argument byte.

### Section A — wall symbols

Compact3 entries; the low three code bits are a **zero-based** symbol id
displayed one-based as `S1..S7`.

### Section B — green blocks (12-byte records)

```text
+0x00  default x_raw     +0x01  default y
+0x02  alternate x_raw   +0x03  alternate y
+0x05..0x09  one-based symbol sequence, zero-terminated
```

Runtime behaviour (capture-verified, modelled in the port):

- correct next symbol → progress advances, that symbol disappears from the block;
- wrong symbol → progress resets, sequence restored;
- full sequence → block toggles default↔alternate position, sequence restored;
- the block owns a 6×2 `0x07` collision footprint at its current position.

Symbols arrive from wall-symbol presses or actor VM `emit_symbol` (opcode
`0x09`, **zero-based** raw id: raw `0` emits `S1`).

### Section C — laser reflectors/crystals

Compact3 entries. Orientation frame = `code & 0x1F` (low five bits only).
Bit `0x80` = self-rotating (advances one frame each time counter `DS:0A20`
counts 10 down, and only while no laser is active — `0x60A9/0x60D2`).
Bit `0x40` = reversed step direction. Controlled reflectors advance one frame
per trigger of their `R` target (`0x6181`). Full reflection physics: see
`gameplay.md`.

### Visual compact3 table

Decor entries with a broad z-order rule: **`code >= 0x80` draws before terrain
(background), `code < 0x80` draws after terrain (foreground)**.

## Coordinate system and blit anchors (EXE-derived, exact)

The room is composed into an offscreen buffer, then a window is scrolled to
the screen. Two blitters only: `0x3CC → 0x1A98` transparent (colour-0 keyed)
for objects, `0x3C9 → 0x1930` opaque copy for backgrounds. Both are pure
top-left blits.

Shared object anchor (covers compact3 decor, header diamonds, apples,
controls, puzzle symbols, actors):

```text
object   buffer (raw_x*2,    raw_y + 0xB8)
terrain  buffer (col*8 + 4,  row*8 + 0xC4)
rope     buffer (col*8 + 8,  row*8 + 0xC8)
```

Editor/screen-space equivalent (buffer cropped at backdrop origin (8, 200)):
object `(raw_x*2 − 8, raw_y − 16)`, terrain `(col*8 − 4, row*8 − 4)`,
rope `(col*8, row*8)`.

The one exception: the **static** platform draw (`0x28AC`) uses
`(x_raw*2 − 4, y + 0xB4)`; the per-frame moving redraw (`0x338A`) uses the
shared anchor.

Actors read a full-resolution 16-bit X at `rec+0x02` (not halved), Y at
`rec+0x04`, and blit at buffer `(x, y + 0xB8)` — same universal anchor.

Puzzle symbol overlays are nudged `+4` px in X (`0x3085`).

## Static draw order (recovered from the room draw path ~`0x2CE2`)

```text
1. backdrop AE001:(30+region) at view origin over a blue clear   (0x2BC0)
   clear colour = VGA index 1 = RGB(0,0,170); backdrop index 1 = transparent
2. compact3 background decor, code >= 0x80                       (0x2BF7)
3. terrain tiles, with rope tiles drawn inside the same
   row-major tile loop                                           (0x2C71/0x2CCF)
4. compact3 foreground decor, code < 0x80                        (0x2D3E)
5. laser crystals (0xD61C) / platforms (0x28AC)
6. header diamonds (0x2E32) / room-gated apple (0x2E89)
7. control buttons, switches, triggers                           (0x2F10)
8. puzzle symbols (0x3085) / green blocks (0x3132)
9. actors, drawn each frame on top                               (0x4EF8)
```

**UNPROVEN**: the region byte that selects the backdrop (`load_room` `0x4517`
reads it from level data as `byte & 0x7F`, but the exact source offset is not
pinned); the exact per-frame z-order of the two animated-decor mechanisms.
