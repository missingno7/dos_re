"""The rasterizer numpy fast paths are BYTE-IDENTICAL to the scalar reference loops.

These rasterizers feed proofs (per-frame RGB sampling in frame verification, the front-end timeline probers,
snapshot evidence PNGs), so the fast path earns its keep only if it changes nothing but wall-clock: same PPM
bytes for every input, including a wrapping CRTC display start and pixel scaling."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import render_frame as rf

numpy = pytest.importorskip("numpy")


def _mem_with_planes(seed: int) -> bytes:
    rng = random.Random(seed)
    mem = bytearray(rf.EGA_APERTURE + 4 * rf.EGA_PLANE_STRIDE)
    mem[rf.EGA_APERTURE:rf.EGA_APERTURE + 4 * rf.EGA_PLANE_STRIDE] = bytes(
        rng.getrandbits(8) for _ in range(4 * rf.EGA_PLANE_STRIDE))
    # a mode-13h image at A000 too (the first 64000 bytes of the aperture region double as the linear test bed)
    mem[0xA0000:0xA0000 + 64000] = bytes(rng.getrandbits(8) for _ in range(64000))
    return bytes(mem)


@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("start", [0, 0x1234, 0xFFC0])   # 0xFFC0: the row read wraps the 16-bit plane offset
def test_planar_numpy_matches_scalar(scale, start):
    mem = _mem_with_planes(start ^ scale)
    fast = rf.render_planar_ppm(mem, start, scale)
    slow = rf._render_planar_ppm_scalar(mem, start, scale)
    assert fast == slow


@pytest.mark.parametrize("scale", [1, 2])
def test_vga_numpy_matches_scalar(scale):
    mem = _mem_with_planes(99)
    pal = [(i & 0xFF, (i * 3) & 0xFF, (i * 7) & 0xFF) for i in range(256)]   # a non-default palette
    fast = rf.render_vga_ppm(mem, 0xA000, scale, palette=pal)
    slow = rf._render_vga_ppm_scalar(mem, 0xA000, scale, palette=pal)
    assert fast == slow
