"""BIOS text-mode (80x25 character/attribute) frame decoder.

Origin: promoted from the Overkill port's viewer (``overkill_port/scripts/
sdl_view.py`` — ``render_text_rgb`` + the ``_DOS_5X7_PATTERNS`` bitmap font),
where it proved out rendering the F9 boss-key BIOS text screen.  The second
consumer was a port whose boot menu is a plain INT 10h/21h text screen that the
graphics-only default decoder rendered black — the promote-on-second-consumer
rule moved it here.

What it decodes is exactly the text-buffer convention :class:`dos_re.dos.
DOSMachine` maintains (``_write_text_cell``): an 80x25 grid of ``char, attr``
byte pairs at B800:0000 (B000:0000 for mono mode 7), with the active display
page selected by ``video_page`` in 0x1000-byte steps.  Each cell becomes an
8x16 pixel block (640x400 output), foreground = ``attr & 0x0F`` and background
= ``(attr >> 4) & 0x07`` through the canonical 16-colour CGA/EGA text palette
(blink is not modelled; bit 7 of the attribute is ignored, matching the origin
renderer).

The glyphs intentionally do not come from a host font: the source screen is a
character-cell bitmap device, and a deterministic ROM-like 5x7 bitmap expanded
into the 8x16 cell keeps the output monospace, crisp and stable across
machines (host/outline fonts made DOS text screens look like scaled UI text).
CP437 box/extended glyphs fall back to ``'?'`` until a real program needs
them; lowercase maps onto the uppercase forms.

numpy is imported lazily, mirroring ``dos_re.player.decode_frame_default`` —
importing this module (or ``dos_re``) must not pull numpy in.
"""
from __future__ import annotations

#: BIOS video modes whose display is the character/attribute text buffer.
TEXT_MODES = (0x00, 0x01, 0x02, 0x03, 0x07)

TEXT_COLS, TEXT_ROWS = 80, 25
CELL_WIDTH, CELL_HEIGHT = 8, 16
TEXT_WIDTH, TEXT_HEIGHT = TEXT_COLS * CELL_WIDTH, TEXT_ROWS * CELL_HEIGHT

#: The canonical CGA/EGA/VGA 16-colour text palette.
TEXT_PALETTE = (
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
)

# ROM-like 5x7 glyph bitmaps (row strings, MSB left), doubled vertically into
# the 8x16 cell by glyph_mask().  Carried over verbatim from the Overkill
# port's viewer.
_DOS_5X7_PATTERNS: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    '"': ("01010", "01010", "01010", "00000", "00000", "00000", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "01010"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "%": ("11001", "11010", "00100", "01000", "01011", "10011", "00000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "00100", "01000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01111"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00001", "00001", "00001", "00001", "10001", "10001", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "\\": ("10000", "01000", "00100", "00010", "00001", "00000", "00000"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "^": ("00100", "01010", "10001", "00000", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "`": ("01000", "00100", "00010", "00000", "00000", "00000", "00000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "~": ("00000", "00000", "01000", "10101", "00010", "00000", "00000"),
}

_GLYPH_CACHE: dict[int, object] = {}


def is_text_display(dos) -> bool:
    """True when the DOS/BIOS video state says the character buffer is what is
    on screen (a BIOS text mode is set and no direct-graphics path overrode it)."""
    return bool(getattr(dos, "text_mode_active", False)) \
        and (getattr(dos, "video_mode", 0xFF) & 0x7F) in TEXT_MODES


def glyph_mask(ch: int):
    """Return the (CELL_HEIGHT, CELL_WIDTH) bool ndarray mask for a character.

    Lowercase ASCII maps to the uppercase form; anything outside printable
    ASCII (CP437 box/extended glyphs) falls back to ``'?'``.
    """
    import numpy as np

    ch &= 0xFF
    cached = _GLYPH_CACHE.get(ch)
    if cached is not None:
        return cached
    if 0x61 <= ch <= 0x7A:
        key = chr(ch - 0x20)
    elif 0x20 <= ch <= 0x7E:
        key = chr(ch)
    else:
        key = "?"
    rows = _DOS_5X7_PATTERNS.get(key, _DOS_5X7_PATTERNS["?"])
    mask = np.zeros((CELL_HEIGHT, CELL_WIDTH), dtype=bool)
    y0 = 1  # centre the 5x14 ink block inside the 8x16 cell
    x0 = 1
    for src_y, row_bits in enumerate(rows):
        for src_x, bit in enumerate(row_bits):
            if bit == "1":
                mask[y0 + src_y * 2:y0 + src_y * 2 + 2, x0 + src_x] = True
    _GLYPH_CACHE[ch] = mask
    return mask


def render_text_rgb(mem, mode: int, page: int = 0):
    """Decode BIOS 80x25 text memory to a native 640x400 HxWx3 uint8 image.

    ``mem`` is the flat physical memory (``rt.cpu.mem.data`` or any buffer
    covering the B000h/B800h window); ``mode`` selects the buffer base
    (mode 7 -> B000:0000, else B800:0000) and ``page`` the 0x1000-byte
    display page.  NUL cells render as blanks (matching what a real adapter
    shows for a zeroed buffer).
    """
    import numpy as np

    base = 0xB0000 if (mode & 0x7F) == 0x07 else 0xB8000
    page_off = (page & 0x07) * 0x1000
    arr = np.zeros((TEXT_HEIGHT, TEXT_WIDTH, 3), dtype=np.uint8)
    mem_arr = np.frombuffer(mem, dtype=np.uint8)
    for row in range(TEXT_ROWS):
        y = row * CELL_HEIGHT
        for col in range(TEXT_COLS):
            x = col * CELL_WIDTH
            off = base + page_off + ((row * TEXT_COLS + col) * 2)
            if off + 1 >= mem_arr.size:
                continue
            ch = int(mem_arr[off]) or 0x20
            attr = int(mem_arr[off + 1])
            fg = TEXT_PALETTE[attr & 0x0F]
            bg = TEXT_PALETTE[(attr >> 4) & 0x07]
            cell = arr[y:y + CELL_HEIGHT, x:x + CELL_WIDTH]
            cell[:, :] = bg
            cell[glyph_mask(ch)] = fg
    return arr


def decode_text_frame(rt):
    """Render the runtime's current text screen (see :func:`render_text_rgb`)."""
    return render_text_rgb(rt.cpu.mem.data, rt.dos.video_mode & 0x7F,
                           getattr(rt.dos, "video_page", 0))
