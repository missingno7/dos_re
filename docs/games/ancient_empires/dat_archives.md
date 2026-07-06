# AE000.DAT / AE001.DAT — resource archive format

Both archives use the same container format. All values little-endian.
Status: **CANONICAL** — the decoder below round-trips every resource in both
shipped archives and feeds the working renderer/audio pipeline.

## Container layout

```text
uint32_le offsets[count + 1]   ; byte offsets from file start
resource block 0
resource block 1
...
```

- The first 32-bit word is the offset of resource 0, so
  `count = first_offset / 4 - 1` (the table includes one trailing
  end-of-last-resource offset; resource i spans `offsets[i]..offsets[i+1]`).
- Offsets are monotonically increasing; the last offset is the end of the
  final resource.

Validation rules used by the proven reader: `first_offset % 4 == 0`,
`first_offset >= 4`, table sorted ascending, last offset `<= file size`.

## Resource block

```text
+0  byte rtype    ; resource type marker
+1  byte flags    ; compression flags
+2  payload[]
```

Decompression stages, applied **in this order**:

```text
flags & 0x02  -> LZW-like stage
flags & 0x01  -> RLE stage
flags == 0    -> payload is plain
```

## RLE stage (flag bit 0)

Signed-count RLE:

```text
control = next byte, interpreted as signed 8-bit
if control > 0:   copy `control` literal bytes from input
if control <= 0:  read one byte, repeat it (-control + 1) times
```

## LZW-like stage (flag bit 1)

Not generic LZW — a custom variant confirmed against the EXE loader routine:

- **Bitstream**: MSB-first bit reader. The first **two bytes** of the payload
  are a stream header/count; the actual bitstream starts at byte 2. The reader
  treats the stream as ending one byte early (a `remaining = len - 1` counter;
  hitting it terminates decode cleanly).
- **Code width**: starts at 9 bits.
- **Code `0x100`**: increase code width by 1 (terminate if width would
  exceed 16). It is a width-escape, not a dictionary-clear.
- **Codes `0x00..0xFF`**: emit that literal byte.
- **Codes `0x101+`**: back-reference into the *already emitted output*.
  The decoder records an output-offset checkpoint before **every pair of
  codes**; reference code `c` with `idx = c - 0x101` copies the output slice
  `out[checkpoint[idx] : checkpoint[idx+1]]` to the end of the output.
  (I.e. the "dictionary" is the sequence of two-code emission spans.)

Decode loop shape (proven implementation):

```text
loop:
    checkpoints.append(len(out))
    code = read(width)        # skipping any 0x100 width bumps
    emit(code)
    code = read(width)        # skipping any 0x100 width bumps
    emit(code)
```

EOF (bit reader exhausted) at any read point ends the stream normally.

## Resource types observed

| rtype | Meaning |
|---:|---|
| `0x00` | Linear sequence of type-`0x47` graphics records (sprite bank) |
| `0x01` | 16-bit offset table pointing at `0x47` graphics records (sprite bank); `AE001:034` uses this table form but with monochrome symbol records instead of `0x47` records |
| `0x44` | Audio container family (PC-speaker SFX bank, PC-speaker music, sound-card music — see `audio.md`) |
| `0x47` | Single direct bitmap record (see `graphics.md`) |
| `0x4D` | Level resource (magic first byte `0x4D`; see `levels.md`) |
| `0x68` | Observed on `AE001` resources 35..62; associated with levels ("level scripts" in the source project's notes). Internal format **UNPROVEN**. |

## Archive contents overview

- **AE000.DAT** — global assets: player/actor/projectile sprites, UI, HUD,
  ropes, switches, pickups, platforms, fonts, map screen art, music, the
  `play_sound` SFX bank, and 27-byte named save/high-score records
  (`AE000:061/062`).
- **AE001.DAT** — the 20 level resources (0..19), the special answer-puzzle
  room (20), terrain banks (21..24), theme decor banks (25..28), region
  backdrops (30+region), the answer-symbol bank (34), and per-artifact puzzle
  images (65+).

See the per-domain docs for the confirmed resource-id maps.

## Write-back note (from the editor)

Rewriting a resource uncompressed (`flags = 0`, payload = decoded bytes) is
accepted by the game's loader — the source project's editor saves edited level
resources this way and preserves untouched blocks byte-for-byte, rebuilding
the offset table as `(len(blocks)+1)*4` header + cumulative offsets.
