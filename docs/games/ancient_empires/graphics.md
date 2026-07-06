# Graphics formats and palettes

Status: **CANONICAL** unless flagged. The decoders described here render every
sprite/terrain/backdrop bank in both archives and match in-game captures.

## Type `0x47` bitmap record

The universal sprite/image format. As stored inside a bank (rtype `0x00`
sequence or rtype `0x01` offset table), each record is:

```text
+0x00  byte 0x47          ; marker
+0x01  byte               ; unused/control
+0x02  byte[16]           ; EGA colour table   (payload 0x00..0x0F)
+0x12  byte[16]           ; VGA colour table   (payload 0x10..0x1F)
+0x22  byte row_bytes     ; (payload 0x20)
+0x23  byte height        ; (payload 0x21)
+0x24  byte[row_bytes*height]  ; packed pixels (payload 0x22..)
```

(The "payload 0xNN" offsets are after stripping the two leading marker bytes —
both conventions appear in the source project; the column above is on-disk.)

- Pixels: **two 4-bit logical colours per byte**, high nibble first.
  Image width in pixels = `row_bytes * 2`.
- **Logical colour 0 is the transparent blit key** for sprites.
- The 36-byte header carries **no hotspot/anchor** — all blits are pure
  top-left (anchors are the caller's job; see `levels.md`).

### Colour resolution per display mode

- **VGA**: `pixel -> vga_table[logical] -> index into the custom 256-colour
  DAC palette` embedded in `AEPROG.EXE` (below).
- **EGA**: `pixel -> ega_table[logical] & 0x0F -> EGA palette register`
  (after the game's register remaps, below).
- **CGA**: encoded in the *same* first 16-byte table — the **high nibble** of
  each EGA-table byte is half of a repeated 2-bit CGA pixel pattern:
  `0x0→colour 0, 0x5→1, 0xA→2, 0xF→3` (duplicate the nibble, take low two
  bits). Displayed through standard CGA palette 1 high-intensity
  (black/cyan/magenta/white). The artists chose the 4-colour mapping per
  image — do **not** derive CGA by nearest-colour reduction.

## VGA DAC palette (embedded in AEPROG.EXE)

The VGA init calls INT 10h `AX=1012h, BX=0, CX=256` with `ES:DX = DS:011E`.

Extraction, proven:

```text
image  = EXE bytes after the MZ header (header_paragraphs*16)
dgroup = 16-bit word at image offset 1   ; first relocated word = DGROUP (0x0FA3)
palette_offset = dgroup*16 + 0x011E      ; 768 bytes, 6-bit DAC triples (all < 64)
rgb8 = round(dac * 255 / 63)
```

Standard VGA palettes do **not** match — always use the embedded one.

## EGA palette register remaps

Video init issues BIOS calls of the form
`mov ax,1000h / mov bx,ccrr / int 10h` (byte pattern
`B8 00 10 BB rr cc CD 10`; `BL` = palette register, `BH` = 6-bit EGA colour).
Scanning the loaded image for that pattern recovers the remaps; in the shipped
EXE this makes logical colour 1 black and colour 15 yellow (register 15 gets
selector `0x16`).

EGA selector→RGB rule (proven against in-game art): a channel is **on (255)**
if *either* its primary or secondary EGA line bit is set —
R: bits `0x04|0x20`, G: `0x02|0x10`, B: `0x01|0x08`. (Treating the selector as
a linear 64-colour DAC renders register 15 wrong.)

## Bitmap fonts

Fonts are ordinary compressed resources (AE000 resources 0 and 1, loaded at
startup by `0x21A9`). Decoded blob:

```text
+0  unused byte
+1  glyph_count - 1        ; byte
+2  line_height            ; byte (pixels)
+3  width[count]           ; per-glyph advance width, indexed by char code
    offset_lo[count]       ; low byte of glyph bitmap offset
    offset_hi[count]       ; high byte of glyph bitmap offset
    bitmap[...]            ; packed 1bpp glyph rows
```

Text routines: `0x6CA6` select_font (caches pointers in `DS:C0DE..C0EA`),
`0x6CF6` measure_string (sums widths per line, returns widest; centring is
`0xA0 - width/2`), `0x6D3C` draw_string (NUL-terminated; `0x0A`/`0x0D` start a
new line advancing `line_height`), `0x17A4` glyph blitter (ORs 1bpp rows into
the planar buffer using text colour `DS:40C8`).

## Monochrome answer-symbol bank — `AE001:034`

Rtype `0x01` offset table, but entries are **not** `0x47` records: each entry
is a 4-byte header followed by a **40×29 1-bit bitmap** (5 bytes/row,
MSB-first). 182 symbols, drawn by the exit-door answer puzzle (`0x9A0E`).

## Confirmed graphics resource map

### AE001

| Resource | Contents |
|---|---|
| 000..019 | The 20 level resources |
| 020 | Special `0x2750`-byte room resource for the exit-door answer puzzle |
| 021..024 | Terrain sprite banks, themes 0..3 (sprite 0 = the conditional exit door) |
| 025..028 | Theme visual/decor banks |
| 030..033 | Play-field backdrops, regions 0..3 (~304×131) and answer-puzzle room frames |
| 034 | Monochrome answer-symbol bank (see above) |
| 035..062 | rtype `0x68` level-associated resources (format unproven) |
| 063..114 | Backdrops for regions 33..84 (selected as resource `30 + region`) |
| 063 | Ancient-Artifact puzzle panel background |
| 064 | Artifact-puzzle Explorer/Expert instruction bands |
| 065+ | Artifact image/title: `65 + chamber + region*8 + (4 if Expert)` |

### AE000

| Resource | Contents |
|---|---|
| 000, 001 | Bitmap fonts 0 and 1 |
| 004 | Player frames (Explorer); 12..15 exit-door walk-through, 16..19 post-collection animation |
| 026 | Map screen backing image |
| 028 | Visible world map |
| 029..032 | Region icons (normal) |
| 033..036 | Region icons (completed) |
| 037 | Map red selector rectangle |
| 044 | Artifact/diamond pickup sprite |
| 049 / 050 | Map music pair (PC-speaker / sound-card) |
| 054, 068, 120, 124 | Sound-card music resources |
| 061, 062 | 27-byte named save/high-score/progress records (NOT instrument banks) |
| 063 | HUD image bank (sprite 0 = HUD frame, 1 = artifact segment, 3..5 = tools, 6..10 = immortality counts, 11..15 = region labels, 16..19 = cavern numerals) |
| 065 | CAF1 `play_sound` PC-speaker SFX bank |

Everything else: actors, ropes, switches, pickups, projectiles, moving
platforms live in AE000; terrain/decor in AE001 (source-qualify sprite ids as
`AE000:nnn` / `AE001:nnn` — gameplay sprites are split across both).
