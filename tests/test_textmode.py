"""Focused tests for dos_re.textmode — the BIOS 80x25 text-mode frame decoder.

Synthetic and game-free: cells are written straight into the B800h/B000h
character/attribute buffer (the same layout ``DOSMachine._write_text_cell``
maintains), decoded, and checked for geometry, glyph ink and attribute
colours.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from dos_re.memory import Memory
from dos_re.player import decode_frame_default
from dos_re.textmode import (
    CELL_HEIGHT,
    CELL_WIDTH,
    TEXT_HEIGHT,
    TEXT_PALETTE,
    TEXT_WIDTH,
    decode_text_frame,
    is_text_display,
    render_text_rgb,
)


def _runtime(mode: int = 0x03, page: int = 0, text_active: bool = True):
    mem = Memory()
    dos = SimpleNamespace(video_mode=mode, video_page=page,
                          text_mode_active=text_active)
    return SimpleNamespace(cpu=SimpleNamespace(mem=mem), dos=dos)


def _put_cell(mem, row: int, col: int, ch: int, attr: int,
              *, base: int = 0xB8000, page: int = 0) -> None:
    off = base + (page & 0x07) * 0x1000 + ((row * 80 + col) * 2)
    mem.data[off] = ch & 0xFF
    mem.data[off + 1] = attr & 0xFF


def _cell(frame, row: int, col: int):
    return frame[row * CELL_HEIGHT:(row + 1) * CELL_HEIGHT,
                 col * CELL_WIDTH:(col + 1) * CELL_WIDTH]


def test_geometry_is_80x25_cells_of_8x16():
    frame = decode_text_frame(_runtime())
    assert frame.shape == (TEXT_HEIGHT, TEXT_WIDTH, 3)
    assert frame.shape == (25 * 16, 80 * 8, 3)
    assert frame.dtype == np.uint8
    # A zeroed buffer (NUL cells, attr 0) renders fully black.
    assert not frame.any()


def test_glyph_ink_lands_inside_its_cell_with_attribute_colours():
    rt = _runtime()
    _put_cell(rt.cpu.mem, 2, 5, ord("A"), 0x1E)   # yellow on blue
    frame = decode_text_frame(rt)

    cell = _cell(frame, 2, 5)
    fg = np.array(TEXT_PALETTE[0x0E], dtype=np.uint8)   # yellow
    bg = np.array(TEXT_PALETTE[0x01], dtype=np.uint8)   # blue
    ink = (cell == fg).all(axis=2)
    paper = (cell == bg).all(axis=2)
    assert ink.any(), "glyph ink missing"
    assert paper.any(), "background missing"
    assert (ink | paper).all(), "cell contains colours outside fg/bg"

    # Everything outside the written cell is a NUL cell -> black.
    outside = frame.copy()
    outside[2 * CELL_HEIGHT:3 * CELL_HEIGHT, 5 * CELL_WIDTH:6 * CELL_WIDTH] = 0
    assert not outside.any()


def test_default_grey_on_black_text_is_non_black():
    rt = _runtime()
    for i, ch in enumerate(b"HELLO"):
        _put_cell(rt.cpu.mem, 0, i, ch, 0x07)
    frame = decode_text_frame(rt)
    grey = np.array(TEXT_PALETTE[0x07], dtype=np.uint8)
    assert ((frame == grey).all(axis=2)).sum() > 0
    # Ink only in the five written cells.
    assert not frame[:, 5 * CELL_WIDTH:].any()
    assert not frame[CELL_HEIGHT:, :].any()


def test_space_and_nul_render_as_plain_background():
    rt = _runtime()
    _put_cell(rt.cpu.mem, 0, 0, 0x00, 0x70)   # NUL, black on grey
    _put_cell(rt.cpu.mem, 0, 1, 0x20, 0x70)   # space, black on grey
    frame = decode_text_frame(rt)
    grey = np.array(TEXT_PALETTE[0x07], dtype=np.uint8)
    for col in (0, 1):
        cell = _cell(frame, 0, col)
        assert (cell == grey).all(), "NUL/space must be pure background"


def test_background_uses_only_three_bits_no_blink_bit():
    rt = _runtime()
    _put_cell(rt.cpu.mem, 0, 0, ord("X"), 0xF1)   # blink+white bg per hardware
    frame = decode_text_frame(rt)
    cell = _cell(frame, 0, 0)
    bg = np.array(TEXT_PALETTE[0x07], dtype=np.uint8)   # (0xF0 >> 4) & 0x07
    assert ((cell == bg).all(axis=2)).any()


def test_video_page_selects_the_0x1000_window():
    rt = _runtime(page=1)
    _put_cell(rt.cpu.mem, 0, 0, ord("B"), 0x07, page=1)
    _put_cell(rt.cpu.mem, 0, 0, ord("Z"), 0x4F, page=0)  # must NOT show
    frame = decode_text_frame(rt)
    cell = _cell(frame, 0, 0)
    grey = np.array(TEXT_PALETTE[0x07], dtype=np.uint8)
    assert ((cell == grey).all(axis=2)).any()
    red_bg = np.array(TEXT_PALETTE[0x04], dtype=np.uint8)
    assert not ((frame == red_bg).all(axis=2)).any()


def test_mono_mode7_reads_b000():
    rt = _runtime(mode=0x07)
    _put_cell(rt.cpu.mem, 3, 3, ord("M"), 0x07, base=0xB0000)
    frame = decode_text_frame(rt)
    assert _cell(frame, 3, 3).any()
    # And the colour buffer is ignored in mode 7.
    rt2 = _runtime(mode=0x07)
    _put_cell(rt2.cpu.mem, 3, 3, ord("M"), 0x07, base=0xB8000)
    assert not decode_text_frame(rt2).any()


def test_lowercase_maps_to_uppercase_glyphs():
    rt_lower, rt_upper = _runtime(), _runtime()
    _put_cell(rt_lower.cpu.mem, 0, 0, ord("g"), 0x07)
    _put_cell(rt_upper.cpu.mem, 0, 0, ord("G"), 0x07)
    assert (decode_text_frame(rt_lower) == decode_text_frame(rt_upper)).all()


def test_is_text_display_gate():
    assert is_text_display(SimpleNamespace(video_mode=0x03, text_mode_active=True))
    assert is_text_display(SimpleNamespace(video_mode=0x83, text_mode_active=True))
    assert not is_text_display(SimpleNamespace(video_mode=0x03, text_mode_active=False))
    assert not is_text_display(SimpleNamespace(video_mode=0x13, text_mode_active=False))
    assert not is_text_display(SimpleNamespace())


def test_decode_frame_default_routes_text_modes():
    rt = _runtime()
    _put_cell(rt.cpu.mem, 7, 20, ord("T"), 0x0A)
    frame = decode_frame_default(rt)
    assert frame.shape == (TEXT_HEIGHT, TEXT_WIDTH, 3)
    assert frame.any()


def test_decode_frame_default_keeps_graphics_path_when_text_inactive():
    rt = _runtime(mode=0x13, text_active=False)
    rt.dos.vga_palette = None
    frame = decode_frame_default(rt)
    assert frame.shape == (200, 320, 3)


def test_render_text_rgb_accepts_plain_buffers():
    buf = bytearray(0xC0000)
    off = 0xB8000
    buf[off], buf[off + 1] = ord("Q"), 0x1F
    frame = render_text_rgb(buf, 0x03)
    assert frame.shape == (TEXT_HEIGHT, TEXT_WIDTH, 3)
    blue = np.array(TEXT_PALETTE[0x01], dtype=np.uint8)
    assert ((_cell(frame, 0, 0) == blue).all(axis=2)).any()
